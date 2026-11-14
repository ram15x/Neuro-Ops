from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_ping():
    assert client.get("/ping").json()["status"] == "ok"


def test_login_wrong_password_is_rejected():
    r = client.post("/api/v1/auth/login", data={"username": "admin", "password": "definitely-wrong"})
    assert r.status_code == 401


def test_protected_route_needs_token():
    r = client.get("/api/v1/auth/me")
    assert r.status_code in (401, 403)


def test_inframind_predict_answers():
    r = client.post(
        "/api/v1/inframind/predict",
        json={"metric": "ec2_cpu", "value": 85.5, "rolling_mean": 45.2, "rolling_std": 12.3, "value_diff": 15.2},
    )
    # open or auth-gated, but it must not crash
    assert r.status_code in (200, 401, 403)
