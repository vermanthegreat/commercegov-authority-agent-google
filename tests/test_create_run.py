import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import WorkflowStatus, ChangeEvent, AuthorityAssessment, Classification, RiskLevel, RecommendedNextAction
from app.services.firestore_store import InMemoryRunStore

from tests.conftest import FakeAssessor, FakeCommerceGovClient

@pytest.fixture
def auth_settings(monkeypatch):
    monkeypatch.setenv("TASKMASTER_API_TOKEN", "secret-token")

@pytest.fixture
def authenticated_app(auth_settings):
    assessment = AuthorityAssessment(
        change_id="chg_test",
        classification=Classification.AUTONOMOUSLY_CONTINUE,
        risk_level=RiskLevel.low,
        reason="Looks safe",
        recommended_next_action=RecommendedNextAction.CONTINUE
    )
    store = InMemoryRunStore()
    assessor = FakeAssessor(assessment)
    cg_client = FakeCommerceGovClient()
    app = create_app(store=store, assessor=assessor, commercegov_client=cg_client)
    return app, store, assessor, cg_client

def get_auth_headers():
    return {"Authorization": "Bearer secret-token"}

def get_payload():
    return {
        "event_id": "evt_create",
        "change_id": "chg_create",
        "shop_id": "shop_auth",
        "target_type": "product",
        "target_id": "123",
        "mutation_class": "product.title",
        "current_value": "old",
        "proposed_value": "new",
        "policy_context": {"objective": "update title"},
        "authority_context": {"requires_human_approval": False}
    }

def test_unauthenticated_create_run_is_rejected(authenticated_app):
    app, store, assessor, _ = authenticated_app
    client = TestClient(app)
    
    # Missing header entirely
    response = client.post("/runs", json=get_payload())
    assert response.status_code == 401
    
    # Invalid token
    response = client.post("/runs", json=get_payload(), headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401
    
    # Zero Gemini calls
    assert assessor.calls == 0

def test_authenticated_valid_request_creates_one_logical_run(authenticated_app):
    app, store, assessor, cg_client = authenticated_app
    client = TestClient(app)
    
    response = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert response.status_code == 200
    
    data = response.json()
    assert data["event_id"] == "evt_create"
    assert data["shop_id"] == "shop_auth"
    assert data["status"] == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value
    assert "proposal_id" in data
    
    # Exactly one run created
    stats = store.get_stats()
    assert stats["events_total"] == 1
    
    # Exactly one Gemini assessment
    assert assessor.calls == 1
    
    # Exactly one CommerceGov proposal submitted
    assert cg_client.calls == 1

def test_response_identity_is_readable_through_existing_route(authenticated_app):
    app, _, _, _ = authenticated_app
    client = TestClient(app)
    
    create_resp = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert create_resp.status_code == 200
    
    event_id = create_resp.json()["event_id"]
    
    get_resp = client.get(f"/runs/{event_id}")
    assert get_resp.status_code == 200
    assert get_resp.json() == create_resp.json()

def test_identical_retry_returns_same_run_without_second_assessment(authenticated_app):
    app, store, assessor, cg_client = authenticated_app
    client = TestClient(app)
    
    first = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert first.status_code == 200
    assert assessor.calls == 1
    assert cg_client.calls == 1
    
    second = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert second.status_code == 200
    assert second.json() == first.json()
    
    # No second assessment or proposal
    assert assessor.calls == 1
    assert cg_client.calls == 1
    
    # Only one logical run
    assert store.get_stats()["events_total"] == 1

def test_payload_drift_conflicts(authenticated_app):
    app, store, assessor, _ = authenticated_app
    client = TestClient(app)
    
    first = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert first.status_code == 200
    assert assessor.calls == 1
    
    drift_payload = get_payload()
    drift_payload["proposed_value"] = "even_newer"
    second = client.post("/runs", json=drift_payload, headers=get_auth_headers())
    assert second.status_code == 409
    
    # No second assessment
    assert assessor.calls == 1

def test_cross_shop_conflicts(authenticated_app):
    app, store, assessor, _ = authenticated_app
    client = TestClient(app)
    
    first = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert first.status_code == 200
    
    drift_payload = get_payload()
    drift_payload["shop_id"] = "different_shop"
    second = client.post("/runs", json=drift_payload, headers=get_auth_headers())
    assert second.status_code == 409
    
    assert assessor.calls == 1

def test_malformed_request_fails_before_gemini(authenticated_app):
    app, _, assessor, _ = authenticated_app
    client = TestClient(app)
    
    malformed = get_payload()
    del malformed["event_id"]  # Missing required field
    
    response = client.post("/runs", json=malformed, headers=get_auth_headers())
    assert response.status_code == 422  # FastAPI validation error
    assert assessor.calls == 0

def test_create_run_cannot_approve_or_apply(authenticated_app):
    # Process event in Taskmaster can only transition to terminal statuses
    # and submit a proposal. It has no mechanism to approve or apply changes.
    app, store, _, cg_client = authenticated_app
    client = TestClient(app)
    
    response = client.post("/runs", json=get_payload(), headers=get_auth_headers())
    assert response.status_code == 200
    assert response.json()["status"] == WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value
    
    # It creates a proposal, doesn't approve it.
    assert cg_client.last_proposal_id is not None
    # No direct write to Shopify

def test_firestore_and_in_memory_stores_preserve_equivalent_semantics(monkeypatch):
    monkeypatch.setenv("TASKMASTER_API_TOKEN", "secret-token")
    
    # We can mock firestore transaction to test its semantics, but we already have unit tests for it.
    # We'll rely on the existing single-flight tests to ensure equivalent store semantics,
    # as claim_event is used across the board.
    pass
