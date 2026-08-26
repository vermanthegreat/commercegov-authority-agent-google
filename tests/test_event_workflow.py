import logging

from fastapi.testclient import TestClient

from app.models import Classification, RecommendedNextAction, WorkflowStatus


def test_human_boundary_persists_waiting_state(app_with_fake, event_payload):
    app, _, assessor, cg_client = app_with_fake
    response = TestClient(app).post("/events/change", json=event_payload)
    assert response.status_code == 200
    assert response.json()["status"] == WorkflowStatus.HUMAN_AUTHORITY_REQUIRED.value
    assert assessor.calls == 1


def test_human_requirement_cannot_be_bypassed_by_model(app_with_fake, event_payload):
    app, _, assessor, cg_client = app_with_fake
    assessor.result = assessor.result.model_copy(update={
        "classification": Classification.READY_FOR_GOVERNED_EXECUTION,
        "recommended_next_action": RecommendedNextAction.CONTINUE,
    })
    response = TestClient(app).post("/events/change", json=event_payload)
    assert response.status_code == 200
    assert response.json()["status"] == WorkflowStatus.HUMAN_AUTHORITY_REQUIRED.value
    assert response.json()["classification"] == Classification.HUMAN_AUTHORITY_REQUIRED.value


def test_terminal_replay_does_not_invoke_model_again(app_with_fake, event_payload, caplog):
    app, _, assessor, cg_client = app_with_fake
    client = TestClient(app)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    first = client.post("/events/change", json=event_payload)
    replay = client.post("/events/change", json=event_payload)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert assessor.calls == 1
    assert "authority_terminal_replay event_id=evt_001" in caplog.messages

def test_blocked_classification_persists_blocked_state(app_with_fake, event_payload):
    app, _, assessor, _ = app_with_fake
    assessor.result = assessor.result.model_copy(update={
        "classification": Classification.BLOCKED,
        "recommended_next_action": RecommendedNextAction.BLOCK,
    })
    # Bypass human requirement so it relies purely on model blocked status
    event_payload["authority_context"]["requires_human_approval"] = False
    response = TestClient(app).post("/events/change", json=event_payload)
    assert response.status_code == 200
    assert response.json()["status"] == WorkflowStatus.BLOCKED.value

