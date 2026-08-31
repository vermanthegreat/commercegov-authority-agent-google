import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    AuthorityIntelligenceAssessmentV1,
    ChangeEvent,
    IntelligenceClassification,
    PipelineNamespace,
)
from app.routes.operational import process_operational_event
from app.services.firestore_store import InMemoryRunStore


class FakeIntelligenceAssessor:
    def __init__(self, classification=IntelligenceClassification.NO_ACTION_REQUIRED):
        self.classification = classification
        self.calls = 0
        self.history = []

    async def assess(self, event, history):
        self.calls += 1
        self.history.append(history)
        return AuthorityIntelligenceAssessmentV1(
            classification=self.classification,
            summary="Model assessment",
            reason="Model supplied reason",
            affected_scope="product",
            evidence_refs=[],
            recommended_operator_action="NONE",
        )


def payload(*, event_id="external-change:1", agency_id="agency-a", shop_id="shop-a", field="title", current="External Demo Change", proposed="The Compare at Price Snowboard", event_type="EXTERNAL_PRODUCTION_CHANGE_DETECTED"):
    return {
        "event_id": event_id,
        "change_id": event_id,
        "agency_id": agency_id,
        "shop_id": shop_id,
        "target_type": "product",
        "target_id": "7887756591181",
        "mutation_class": f"product.{field}",
        "current_value": current,
        "proposed_value": proposed,
        "policy_context": {"event_type": event_type, "governed_field": field},
        "authority_context": {"authority_mode": "PROPOSE_ONLY", "requires_human_approval": True},
    }


def client_for(monkeypatch, assessor):
    monkeypatch.setenv("TASKMASTER_API_TOKEN", "operational-token")
    return TestClient(create_app(store=InMemoryRunStore(), intelligence_assessor=assessor))


def test_operational_auth_missing_invalid_and_valid(monkeypatch):
    assessor = FakeIntelligenceAssessor()
    client = client_for(monkeypatch, assessor)
    assert client.post("/events/operational", json=payload()).status_code == 401
    assert client.post("/events/operational", json=payload(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.post("/events/operational", json=payload(), headers={"Authorization": "Bearer operational-token"})
    assert response.status_code == 200
    assert assessor.calls == 1


@pytest.mark.parametrize("field", ["title", "description"])
def test_external_governed_mismatch_has_authority_risk_floor(monkeypatch, field):
    client = client_for(monkeypatch, FakeIntelligenceAssessor())
    response = client.post("/events/operational", json=payload(field=field), headers={"Authorization": "Bearer operational-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["intelligence_classification"] == "AUTHORITY_AT_RISK"
    assert body["recommended_operator_action"] == "INVESTIGATE_RISK"
    for field_name in ("event_id", "agency_id", "shop_id", "target_type", "target_id", "mutation_class", "status", "summary", "reason", "affected_scope", "evidence_refs", "attention_key"):
        assert body[field_name] is not None


def test_non_external_and_identical_events_are_not_forced(monkeypatch):
    assessor = FakeIntelligenceAssessor()
    client = client_for(monkeypatch, assessor)
    headers = {"Authorization": "Bearer operational-token"}
    ordinary = client.post("/events/operational", json=payload(event_id="ordinary", event_type="INFORMATIONAL"), headers=headers)
    identical = client.post("/events/operational", json=payload(event_id="identical", current="same", proposed="same"), headers=headers)
    assert ordinary.json()["intelligence_classification"] == "NO_ACTION_REQUIRED"
    assert identical.json()["intelligence_classification"] == "NO_ACTION_REQUIRED"


@pytest.mark.asyncio
async def test_event_identity_binding_replay_and_history():
    store = InMemoryRunStore()
    assessor = FakeIntelligenceAssessor(IntelligenceClassification.INFORMATIONAL)
    first = ChangeEvent.model_validate(payload(event_id="bound", agency_id="agency-a", shop_id="shop-a", event_type="INFORMATIONAL"))
    first_run = await process_operational_event(first, store, assessor)
    replay = await process_operational_event(first, store, assessor)
    assert assessor.calls == 1
    assert replay["event_id"] == first_run["event_id"]
    conflicting = first.model_copy(update={"agency_id": "agency-b"})
    with pytest.raises(Exception, match="Event ID conflict"):
        await process_operational_event(conflicting, store, assessor)
    later = first.model_copy(update={"event_id": "bound-later", "proposed_value": "other"})
    await process_operational_event(later, store, assessor)
    assert assessor.history[-1]
    assert first_run["attention_key"] != (await process_operational_event(later.model_copy(update={"event_id": "shop-b", "shop_id": "shop-b"}), store, assessor))["attention_key"]
    assert store.get(PipelineNamespace.AUTHORITY_INTELLIGENCE.value, "bound")["agency_id"] == "agency-a"

def test_operational_route_never_uses_commercegov_client(monkeypatch):
    class ForbiddenCommerceGovClient:
        def __init__(self):
            self.calls = 0

        async def submit_proposal(self, proposal):
            self.calls += 1
            raise AssertionError("Intelligence route must not submit CommerceGov proposals")

    monkeypatch.setenv("TASKMASTER_API_TOKEN", "operational-token")
    forbidden_client = ForbiddenCommerceGovClient()
    client = TestClient(create_app(
        store=InMemoryRunStore(),
        intelligence_assessor=FakeIntelligenceAssessor(),
        commercegov_client=forbidden_client,
    ))
    response = client.post(
        "/events/operational",
        json=payload(event_type="INFORMATIONAL"),
        headers={"Authorization": "Bearer operational-token"},
    )
    assert response.status_code == 200
    assert forbidden_client.calls == 0