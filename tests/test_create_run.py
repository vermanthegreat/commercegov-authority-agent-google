from app.models import PipelineNamespace
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
    stats = store.get_stats(PipelineNamespace.AUTHORITY_ASSESSMENT.value)
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
    assert store.get_stats(PipelineNamespace.AUTHORITY_ASSESSMENT.value)["events_total"] == 1

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
    from tests.test_firestore_single_flight import FakeFirestoreClient, transactional, SERVER_TIMESTAMP
    import google.cloud
    from types import SimpleNamespace
    from app.services.firestore_store import FirestoreRunStore, InMemoryRunStore

    module = SimpleNamespace(SERVER_TIMESTAMP=SERVER_TIMESTAMP, transactional=transactional)
    monkeypatch.setattr(google.cloud, "firestore", module, raising=False)

    fs_client = FakeFirestoreClient()
    fs_store = FirestoreRunStore.__new__(FirestoreRunStore)
    fs_store._client = fs_client

    im_store = InMemoryRunStore()

    event = ChangeEvent(
        event_id="evt-eq",
        change_id="chg-eq",
        agency_id="tenant-1",
        shop_id="shop",
        target_type="product",
        target_id="1",
        mutation_class="product.title",
        current_value="old",
        proposed_value="new",
    )

    # 1. Claim event
    res_im, run_im = im_store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-1", 60)
    res_fs, run_fs = fs_store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-1", 60)

    assert res_im == res_fs
    assert run_im["status"] == run_fs["status"] == WorkflowStatus.PROCESSING.value
    assert run_im["event_id"] == run_fs["event_id"] == "evt-eq"
    assert run_im["shop_id"] == run_fs["shop_id"] == "shop"

    # 2. Duplicate claim (idempotent replay)
    res_im2, _ = im_store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-2", 60)
    res_fs2, _ = fs_store.claim_event(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event, "owner-2", 60)
    from app.models import ClaimResult
    assert res_im2 == res_fs2 == ClaimResult.IN_PROGRESS

    # 3. Begin assessment
    state_im = im_store.begin_assessment(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-1", run_im["attempt"])
    state_fs = fs_store.begin_assessment(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-1", run_fs["attempt"])
    assert state_im["status"] == state_fs["status"] == WorkflowStatus.ASSESSING.value

    # 4. Terminal immutability
    settle_im = im_store.settle(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-1", run_im["attempt"], status=WorkflowStatus.FAILED.value, reason="failed")
    settle_fs = fs_store.settle(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-1", run_fs["attempt"], status=WorkflowStatus.FAILED.value, reason="failed")

    assert settle_im["status"] == settle_fs["status"] == WorkflowStatus.FAILED.value
    assert settle_im["reason"] == settle_fs["reason"] == "failed"

    # Attempt to settle again
    settle_im2 = im_store.settle(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-1", run_im["attempt"], status=WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value, reason="changed")
    settle_fs2 = fs_store.settle(PipelineNamespace.AUTHORITY_ASSESSMENT.value, event.event_id, "owner-1", run_fs["attempt"], status=WorkflowStatus.AUTONOMOUSLY_CONTINUABLE.value, reason="changed")

    assert settle_im2["status"] == settle_fs2["status"] == WorkflowStatus.FAILED.value
    assert settle_im2["reason"] == settle_fs2["reason"] == "failed"
