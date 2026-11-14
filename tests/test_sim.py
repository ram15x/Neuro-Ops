import pandas as pd

from backend.sim.aws_fleet import simulate, FLEET, STEPS_PER_DAY


def test_same_seed_same_data():
    a, b = simulate(days=5, seed=3), simulate(days=5, seed=3)
    assert a.metrics.equals(b.metrics)
    assert [e.to_dict() for e in a.events] == [e.to_dict() for e in b.events]


def test_different_seed_different_data():
    a, b = simulate(days=5, seed=3), simulate(days=5, seed=4)
    assert not a.metrics["value"].equals(b.metrics["value"])


def test_shape_and_ranges():
    r = simulate(days=5, seed=1)
    assert r.simulated is True
    pct = r.metrics[r.metrics.metric.isin(["CPUUtilization", "mem_used_percent", "disk_used_percent"])]
    assert pct["value"].between(0, 100).all()
    one = r.series(FLEET[0][0], "CPUUtilization")
    assert len(one) == 5 * STEPS_PER_DAY
    # cloudwatch-style 5 minute spacing
    assert (one["timestamp"].diff().dropna() == pd.Timedelta(minutes=5)).all()


def test_memory_and_disk_come_from_the_agent_namespace():
    r = simulate(days=4, seed=1)
    ns = r.metrics.groupby("metric")["namespace"].first()
    assert ns["CPUUtilization"] == "AWS/EC2"
    assert ns["mem_used_percent"] == "CWAgent"
    assert ns["disk_used_percent"] == "CWAgent"


def test_only_burstable_instances_have_credits():
    r = simulate(days=4, seed=1)
    with_credits = set(r.metrics[r.metrics.metric == "CPUCreditBalance"].instance_id)
    expected = {iid for iid, _, itype in FLEET if itype.startswith("t3")}
    assert with_credits == expected


def test_faults_do_not_overlap_on_one_instance():
    r = simulate(seed=5)
    by = {}
    for e in r.events:
        by.setdefault(e.instance_id, []).append((e.start, e.end))
    for spans in by.values():
        spans.sort()
        for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
            assert b1 < a2


def test_no_faults_during_warm_up():
    r = simulate(seed=5, warm_days=3)
    first = r.metrics["timestamp"].min() + pd.Timedelta(days=3)
    assert all(pd.Timestamp(e.start) >= first for e in r.events)


def test_ramps_record_when_they_breach():
    r = simulate(seed=7)
    ramps = [e for e in r.events if e.kind in ("memory_leak", "disk_fill")]
    assert ramps
    for e in ramps:
        assert e.start < e.breach_at <= e.end
        s = r.series(e.instance_id, e.metrics[0])
        at = s[s.timestamp == pd.Timestamp(e.breach_at)]["value"].iloc[0]
        assert at >= 90


def test_runaway_cpu_is_visible():
    r = simulate(seed=7)
    e = next(e for e in r.events if e.kind == "cpu_runaway")
    s = r.series(e.instance_id, "CPUUtilization")
    during = s[(s.timestamp >= pd.Timestamp(e.start)) & (s.timestamp < pd.Timestamp(e.end))]["value"]
    assert during.median() > 80
