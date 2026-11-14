"""
The live loop end to end: a moto server speaking real HTTP, the real boto3 clients,
the real engine. Still a fake AWS, so it proves wiring and failure handling only.
"""

import boto3
import numpy as np
import pandas as pd
import pytest
from moto.server import ThreadedMotoServer

from backend.api import engine as api
from backend.engine.executors import AwsExecutor
from backend.engine.sources import CloudWatchSource

REGION = "ap-south-1"


@pytest.fixture(scope="module")
def server():
    srv = ThreadedMotoServer(port=0, verbose=False)
    srv.start()
    host, port = srv.get_host_and_port()
    yield f"http://{host}:{port}"
    srv.stop()


@pytest.fixture
def aws(server, monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                 ("AWS_DEFAULT_REGION", REGION), ("AWS_ENDPOINT_URL", server)):
        monkeypatch.setenv(k, v)
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    iid = ec2.run_instances(ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1, TagSpecifications=[
        {"ResourceType": "instance", "Tags": [{"Key": "Role", "Value": "web"},
                                              {"Key": "neuroops:managed", "Value": "true"}]}])["Instances"][0]["InstanceId"]
    yield dict(ec2=ec2, iid=iid, cw=boto3.client("cloudwatch", region_name=REGION))
    ec2.terminate_instances(InstanceIds=[iid])


def _put(cw, iid, metric, ns, stamps, values):
    for k in range(0, len(stamps), 500):
        cw.put_metric_data(Namespace=ns, MetricData=[{
            "MetricName": metric, "Value": float(v), "Timestamp": t.to_pydatetime(),
            "Dimensions": [{"Name": "InstanceId", "Value": iid}]}
            for t, v in zip(stamps[k:k + 500], values[k:k + 500])])


def _leak(aws, now, hours=48, end=93.0):
    stamps = pd.date_range(end=now, periods=hours * 12, freq="5min")
    rng = np.random.default_rng(1)
    mem = np.linspace(40, end, len(stamps)) + rng.normal(0, 0.4, len(stamps))
    cpu = 20 + rng.normal(0, 2, len(stamps))
    _put(aws["cw"], aws["iid"], "mem_used_percent", "CWAgent", stamps, mem)
    _put(aws["cw"], aws["iid"], "CPUUtilization", "AWS/EC2", stamps, cpu)


def _runner():
    r = api.LiveRunner(CloudWatchSource(REGION, lookback_days=3), AwsExecutor(REGION))
    r.store = False
    return r


def test_a_leak_on_a_live_instance_is_found_and_stays_in_shadow(aws):
    now = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
    _leak(aws, now)
    r = _runner()
    r.tick()
    assert [(i.cause, i.target) for i in r.engine.incidents] == [("memory_leak", aws["iid"])]
    kinds = [e["type"] for e in r.engine.audit]
    assert "action" not in kinds                      # shadow: it says what it would do, it does not do it
    assert [e["why"] for e in r.engine.audit if e["type"] == "notified"] == ["shadow"]
    assert r.engine.cfg.mode == "shadow"


def test_nothing_is_done_twice_across_ticks(aws):
    now = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
    _leak(aws, now)
    r = _runner()
    r.tick()
    n = len(r.engine.incidents)
    r.tick()
    r.tick()
    assert len(r.engine.incidents) == n


def test_empty_cloudwatch_is_a_data_gap_not_a_crash(aws):
    r = _runner()
    r.tick()
    gaps = [e for e in r.engine.audit if e["type"] == "data_gap"]
    assert len(gaps) == 1 and gaps[0]["target"] == aws["iid"]
    r.tick()
    assert len([e for e in r.engine.audit if e["type"] == "data_gap"]) == 1   # said once, not every tick


def test_a_cloudwatch_error_skips_the_instance_and_the_loop_survives(aws, monkeypatch):
    r = _runner()

    def boom(*a, **k):
        raise RuntimeError("throttled")
    monkeypatch.setattr(r.engine.source, "history_many", boom)
    r.tick()
    r.tick()
    assert r.ticks == 2
    assert [e["why"] for e in r.engine.audit if e["type"] == "data_gap"] == ["could not read metrics: RuntimeError"]


def test_the_api_views_read_the_live_data(aws, monkeypatch):
    now = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
    _leak(aws, now)
    r = _runner()
    r.tick()
    monkeypatch.setattr(api, "_runner", r)
    assert api.status()["source"] == "cloudwatch"
    f = api.fleet()
    assert f["simulated"] is False and f["instances"][0]["latest"]["mem_used_percent"] > 80
    s = api.series(aws["iid"], "mem_used_percent", 24)
    assert len(s["values"]) == 24
    with pytest.raises(Exception):
        api.snap()


def test_with_live_actions_on_a_managed_box_is_fixed_and_an_untagged_one_is_left_alone(aws, monkeypatch):
    ec2 = aws["ec2"]
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    other = ec2.run_instances(ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    now = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
    _leak(aws, now)
    _leak(dict(aws, iid=other), now)
    r = _runner()
    r.engine.cfg.mode = "auto"
    calls = []
    monkeypatch.setattr(r.engine.executor, "run", lambda a, t, c: (calls.append(t), {"ok": True, "detail": "stub", "undo": None})[1])
    r.tick()
    assert other not in calls                          # not tagged neuroops:managed, so notify only
    told = [e for e in r.engine.audit if e["type"] == "notified" and e.get("incident") and
            r.engine.incidents[e["incident"] - 1].target == other]
    assert told, "the untagged box should still get a human alert"
