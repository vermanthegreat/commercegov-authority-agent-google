from fastapi.testclient import TestClient


def test_health(app_with_fake):
    app, _, _, _ = app_with_fake
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "commercegov-authority-agent"}
