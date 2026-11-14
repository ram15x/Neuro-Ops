import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.engine.diagnose import diagnose
from backend.engine.engine import Engine
from backend.engine.executors import SimExecutor
from backend.engine.model import Instance, Signal
from backend.engine.policy import PolicyConfig
from backend.engine.sources import SimSource
from backend.sim.aws_fleet import build_fleet

T = pd.Timestamp("2026-03-10 12:00").to_pydatetime()
FLEET = [Instance("web-1", "web", "t3.small"), Instance("web-2", "web", "t3.small"),
         Instance("web-3", "web", "t3.small"), Instance("db-1", "db", "m5.large")]


def sig(iid, metric, kind="anomaly", sev="critical", value=50.0, direction="up"):
    return Signal(T, iid, metric, kind, sev, value, "test", direction)


def causes(signals):
    return {(d.cause, d.target) for d in diagnose(signals, FLEET)}


def test_cpu_up_across_a_tier_is_load_not_three_broken_boxes():
    got = causes([sig("web-1", "CPUUtilization"), sig("web-2", "CPUUtilization"), sig("web-1", "NetworkIn")])
    assert got == {("traffic_surge", "web")}


def test_high_cpu_on_one_box_is_a_runaway_process():
    assert causes([sig("web-1", "CPUUtilization", value=96)]) == {("runaway_process", "web-1")}


def test_mild_cpu_on_one_box_is_contention():
    assert causes([sig("web-1", "CPUUtilization", sev="warning", value=38)]) == {("cpu_contention", "web-1")}


def test_memory_jump_without_a_trend_is_a_release():
    assert causes([sig("web-1", "mem_used_percent", sev="warning")]) == {("memory_step", "web-1")}


def test_memory_forecast_is_a_leak():
    assert causes([sig("web-1", "mem_used_percent", kind="forecast")]) == {("memory_leak", "web-1")}


def test_disk_and_traffic_drop():
    got = causes([sig("db-1", "disk_used_percent", kind="limit"), sig("web-2", "NetworkIn", direction="down")])
    assert got == {("disk_fill", "db-1"), ("network_loss", "web-2")}


@pytest.fixture(scope="module")
def runs():
    out = {}
    for mode in ("shadow", "auto"):
        fleet = build_fleet(days=7, seed=5)
        out[mode] = (fleet, Engine(SimSource(fleet), SimExecutor(fleet), PolicyConfig(mode=mode),
                                   auto_approve_after=3 if mode == "auto" else None).run())
    return out


def test_shadow_mode_changes_nothing(runs):
    fleet, eng = runs["shadow"]
    assert not [a for a in eng.audit if a["type"] == "action"]
    assert all(e.stopped_at is None for e in fleet.events)
    # it still says what it would have done
    assert any(a["type"] == "decision" and a["status"] == "shadow" for a in eng.audit)


def test_auto_mode_cuts_faults_short(runs):
    fleet, eng = runs["auto"]
    stopped = [e for e in fleet.events if e.stopped_at]
    assert stopped
    for e in stopped:
        assert e.start < e.stopped_at <= e.end + pd.Timedelta(minutes=10)


def test_every_action_has_a_decision_and_a_verification(runs):
    _, eng = runs["auto"]
    actions = [a for a in eng.audit if a["type"] == "action" and a["ok"]]
    assert actions
    decided = {(a["incident"], a["action"]) for a in eng.audit if a["type"] in ("decision", "approved")}
    checked = [a for a in eng.audit if a["type"] in ("verified", "verify_failed")]
    for a in actions:
        assert (a["incident"], a["action"]) in decided
    # the last action may still be waiting for its check when the run ends
    assert len(actions) - len(checked) <= 1


def test_risky_actions_only_run_after_approval(runs):
    _, eng = runs["auto"]
    for a in eng.audit:
        if a["type"] == "action" and a["action"] in ("migrate_host", "enable_unlimited"):
            assert a["approved_by"]


def test_everything_in_the_audit_log_is_marked_simulated(runs):
    _, eng = runs["auto"]
    assert eng.audit and all(a["simulated"] for a in eng.audit)


def test_scale_out_is_reverted_when_the_surge_ends(runs):
    _, eng = runs["auto"]
    outs = [a for a in eng.audit if a["type"] == "action" and a["action"] == "scale_out" and a["ok"]]
    backs = [a for a in eng.audit if a["type"] == "rolled_back" and a["action"] == "scale_out"]
    closed = {a["incident"] for a in eng.audit if a["type"] == "incident_resolved"}
    assert len(backs) == len([a for a in outs if a["incident"] in closed])


def test_engine_api(monkeypatch):
    from backend.api import engine as api
    from backend.main import app
    monkeypatch.setattr(api, "_runner", None)
    client = TestClient(app)
    runner = api.get_runner()
    for _ in range(30):
        runner.tick()
    s = client.get("/api/v1/engine/status").json()
    assert s["simulated"] is True and s["mode"] == "shadow" and s["step"] == runner.engine.warm + 30
    fleet = client.get("/api/v1/engine/fleet").json()
    assert len(fleet["instances"]) == 7
    iid = fleet["instances"][0]["id"]
    series = client.get("/api/v1/engine/series", params={"instance_id": iid, "metric": "CPUUtilization", "points": 12}).json()
    assert len(series["values"]) == 12 == len(series["timestamps"])
    assert client.get("/api/v1/engine/series", params={"instance_id": "nope", "metric": "CPUUtilization"}).status_code == 404
    assert client.get("/api/v1/engine/incidents").status_code == 200
    assert client.get("/api/v1/engine/audit").status_code == 200
    # changing mode or answering approvals needs a login
    assert client.post("/api/v1/engine/mode", json={"mode": "auto"}).status_code == 401
    assert client.post("/api/v1/engine/approvals/1/approve").status_code == 401
