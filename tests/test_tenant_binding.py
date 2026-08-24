import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    AuthorityAssessment,
    ChangeEvent,
    ClaimResult,
    Classification,
    RecommendedNextAction,
    RiskLevel,
    WorkflowStatus,
)
from app.services.firestore_store import InMemoryRunStore


def make_event(event_id: str, shop_id: str) -> ChangeEvent:
    return ChangeEvent(
        event_id=event_id,
        change_id=f"change-{event_id}",
        shop_id=shop_id,
        target_type="product",
        target_id="product-1",
        mutation_class="product.title",
        current_value="old",
        proposed_value="new",
        authority_context={"requires_human_approval": False},
    )


def test_in_memory_shop_binding_replay_conflict_and_immutability():
    store = InMemoryRunStore()
    shop_a_event = make_event("evt-shared", "shop-a")

    first_result, first = store.claim_event(shop_a_event, "owner-a")
    replay_result, replay = store.claim_event(shop_a_event, "owner-replay")
    conflict_result, conflict = store.claim_event(
        shop_a_event.model_copy(update={"shop_id": "shop-b"}),
        "owner-b",
    )

    assert first_result == ClaimResult.CLAIM_ACQUIRED
    assert first["shop_id"] == "shop-a"
    assert replay_result == ClaimResult.IN_PROGRESS
    assert replay["event_id"] == first["event_id"]
    assert conflict_result == ClaimResult.EVENT_ID_CONFLICT
    assert conflict["shop_id"] == "shop-a"

    store.begin_assessment(shop_a_event.event_id, "owner-a", first["attempt"])
    with pytest.raises(ValueError, match="shop_id is immutable"):
        store.settle(
            shop_a_event.event_id,
            "owner-a",
            first["attempt"],
            status=WorkflowStatus.FAILED.value,
            shop_id="shop-b",
        )
    assert store.get(shop_a_event.event_id)["shop_id"] == "shop-a"


def test_list_route_requires_and_filters_by_exact_shop_before_pagination():
    store = InMemoryRunStore()
    shop_a_event = make_event("evt-a", "shop-a")
    shop_b_event = make_event("evt-b", "shop-b")
    store.claim_event(shop_a_event, "owner-a")
    store.claim_event(shop_b_event, "owner-b")
    app = create_app(store=store, assessor=CountingAssessor())
    client = TestClient(app)

    assert client.get("/runs").status_code == 422
    assert client.get("/runs", params={"shop_id": ""}).status_code == 422

    shop_a = client.get("/runs", params={"shop_id": "shop-a", "limit": 1})
    shop_b = client.get("/runs", params={"shop_id": "shop-b", "limit": 1})

    assert shop_a.status_code == shop_b.status_code == 200
    assert [run["event_id"] for run in shop_a.json()] == ["evt-a"]
    assert [run["event_id"] for run in shop_b.json()] == ["evt-b"]
    assert {run["shop_id"] for run in shop_a.json()} == {"shop-a"}
    assert {run["shop_id"] for run in shop_b.json()} == {"shop-b"}


def test_detail_exposes_persisted_shop_and_legacy_is_explicitly_unbound():
    store = InMemoryRunStore()
    bound = make_event("evt-bound", "shop-a")
    store.claim_event(bound, "owner-a")
    legacy = {
        "event_id": "evt-legacy",
        "fingerprint": make_event("evt-legacy", "shop-a").fingerprint,
        "status": WorkflowStatus.PROCESSING.value,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    store.runs["evt-legacy"] = legacy
    app = create_app(store=store, assessor=CountingAssessor())
    client = TestClient(app)

    bound_response = client.get("/runs/evt-bound")
    legacy_response = client.get("/runs/evt-legacy")
    filtered = client.get("/runs", params={"shop_id": "shop-a"})

    assert bound_response.status_code == legacy_response.status_code == 200
    assert bound_response.json()["shop_id"] == "shop-a"
    assert legacy_response.json()["shop_id"] is None
    assert [run["event_id"] for run in filtered.json()] == ["evt-bound"]

    legacy_event = make_event("evt-legacy", "shop-a")
    result, unchanged = store.claim_event(legacy_event, "owner-legacy")
    assert result == ClaimResult.EVENT_ID_CONFLICT
    assert "shop_id" not in unchanged
    assert "shop_id" not in store.runs["evt-legacy"]


class CountingAssessor:
    def __init__(self) -> None:
        self.calls = 0

    async def assess(self, event):
        self.calls += 1
        return AuthorityAssessment(
            change_id=event.change_id,
            classification=Classification.AUTONOMOUSLY_CONTINUE,
            risk_level=RiskLevel.low,
            reason="Safe to submit as a governed proposal.",
            policy_observations=[],
            recommended_next_action=RecommendedNextAction.CONTINUE,
        )


class RecordingCommerceGovClient:
    def __init__(self) -> None:
        self.calls = 0
        self.shop_ids = []

    async def submit_proposal(self, shop_id, product_id, changes, idempotency_key):
        self.calls += 1
        self.shop_ids.append(shop_id)
        return f"proposal-{idempotency_key}"


def test_proposal_and_terminal_replay_preserve_single_shop_binding():
    store = InMemoryRunStore()
    assessor = CountingAssessor()
    commercegov = RecordingCommerceGovClient()
    app = create_app(store=store, assessor=assessor, commercegov_client=commercegov)
    client = TestClient(app)
    payload = make_event("evt-proposal", "shop-a").model_dump()

    first = client.post("/events/change", json=payload)
    replay = client.post("/events/change", json=payload)
    detail = client.get("/runs/evt-proposal")

    assert first.status_code == replay.status_code == detail.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["shop_id"] == detail.json()["shop_id"] == "shop-a"
    assert commercegov.shop_ids == ["shop-a"]
    assert assessor.calls == 1
    assert commercegov.calls == 1
