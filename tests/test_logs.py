import pandas as pd

from backend.logs.cluster import cluster_lines, level_of, rising, tokens
from backend.sim.aws_fleet import build_fleet
from backend.sim.logs import fleet_logs


def test_numbers_ids_and_hosts_do_not_make_new_groups():
    lines = [f"GET /orders/{i} 200 {i % 40}ms from 10.0.0.{i % 200}" for i in range(300)]
    assert len(cluster_lines(lines)) == 1


def test_different_messages_stay_apart():
    got = cluster_lines(["disk full on /var", "disk full on /tmp", "connection reset by peer", "user logged in"])
    assert len(got) == 3


def test_masking_catches_uuids_ec2_ids_and_hex():
    t = tokens("started i-0a1b2c3d4e5f6a7b8 req 3f2a9c1e-1111-2222-3333-444455556666 id deadbeefcafe1234")
    assert not any(x in " ".join(t) for x in ("0a1b2c3d", "3f2a9c1e", "deadbeef"))


def test_level_is_read_from_the_line():
    assert level_of("2026-03-01 ERROR write failed") == "error"
    assert level_of("WARN slow query") == "warn"
    assert level_of("all good") == "info"


def test_rising_puts_the_new_error_first_and_drops_normal_traffic():
    normal = [(None, f"GET /health 200 {i}ms") for i in range(480)]
    window = [(None, f"GET /health 200 {i}ms") for i in range(10)] + \
             [(None, f"ERROR write failed: No space left on device path=/var/log/{i}.log") for i in range(4)]
    out = rising(window, normal, baseline_scale=10 / 480)
    assert out[0]["template"].startswith("ERROR write failed") and out[0]["new"]
    assert all("GET /health" not in c["template"] for c in out)


def test_a_quiet_window_reports_nothing():
    normal = [(None, f"GET /health 200 {i}ms") for i in range(480)]
    assert rising(normal[:10], normal, baseline_scale=10 / 480) == []


def test_simulated_fault_lines_show_up_during_the_fault_and_not_before():
    f = build_fleet(seed=21)
    ev = next(e for e in f.events if e.kind == "disk_fill")
    a, b = f.ts.get_loc(pd.Timestamp(ev.start)), f.ts.get_loc(pd.Timestamp(ev.end))
    late = " ".join(l for _, l in fleet_logs(f, ev.instance_id, b - 20, b))
    early = " ".join(l for _, l in fleet_logs(f, ev.instance_id, a - 300, a))
    assert "No space left on device" in late
    assert "No space left on device" not in early


def test_simulated_logs_are_repeatable():
    f = build_fleet(seed=21)
    iid = next(iter(f.instances))
    assert fleet_logs(f, iid, 1000, 1010) == fleet_logs(f, iid, 1000, 1010)


def test_cloudwatch_logs_source_reads_a_stream():
    import boto3
    from moto import mock_aws
    from backend.engine.sources import CloudWatchSource
    with mock_aws():
        c = boto3.client("logs", region_name="us-east-1")
        c.create_log_group(logGroupName="app")
        c.create_log_stream(logGroupName="app", logStreamName="i-abc")
        base = pd.Timestamp.utcnow().tz_localize(None).floor("h")   # moto refuses events older than two weeks
        t = int(base.timestamp() * 1000)
        c.put_log_events(logGroupName="app", logStreamName="i-abc",
                         logEvents=[{"timestamp": t + i * 1000, "message": f"ERROR boom {i}"} for i in range(3)])
        src = CloudWatchSource("us-east-1", log_group="app")
        got = src.logs("i-abc", base - pd.Timedelta(hours=1), base + pd.Timedelta(hours=1))
        assert [m for _, m in got] == ["ERROR boom 0", "ERROR boom 1", "ERROR boom 2"]
        assert CloudWatchSource("us-east-1").logs("i-abc", 0, 1) == []
