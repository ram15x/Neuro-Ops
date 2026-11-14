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


def test_old_v2_endpoints_are_off_by_default():
    r = client.post("/api/v1/inframind/predict", json={"metric": "ec2_cpu", "value": 85.5})
    assert r.status_code == 404


def test_engine_history_endpoint_answers():
    r = client.get("/api/v1/engine/history?limit=5")
    assert r.status_code == 200
    assert "events" in r.json()


def test_stored_audit_events_come_back_from_history():
    from backend.api.engine import Runner
    r = Runner()
    r._store_event({"ts": "2026-03-10T12:00:00", "type": "mode_changed", "simulated": True, "old": "shadow", "new": "auto"})
    got = client.get(f"/api/v1/engine/history?run={r.seed}&limit=5").json()["events"]
    assert got[0]["type"] == "mode_changed" and got[0]["new"] == "auto"


def test_metrics_endpoint_speaks_prometheus():
    body = client.get("/metrics").text
    for name in ("neuroops_incidents_open", "neuroops_mode", "neuroops_tick_seconds", "neuroops_alerts_total"):
        assert f"# TYPE {name} " in body
    assert 'neuroops_mode{mode="shadow"} 1' in body


def test_report_and_postmortem_endpoints():
    r = client.get("/api/v1/engine/report")
    assert r.status_code == 200 and r.json()["simulated"] is True
    assert client.get("/api/v1/engine/incidents/99999/postmortem").status_code == 404
