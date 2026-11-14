# the dashboard reads these snapshots, so the indexes in them have to line up
import pytest

from eval.export_replay import LEAD_IN, record


@pytest.fixture(scope="module")
def rec():
    return record("clean", "auto")


def test_every_series_has_one_point_per_step(rec):
    n = rec["meta"]["steps"]
    for iid, m in rec["metrics"].items():
        for name, vals in m.items():
            assert len(vals) == n, (iid, name)


def test_indexes_stay_inside_the_recording(rec):
    n = rec["meta"]["steps"]
    for e in rec["audit"]:
        assert 0 <= e["i"] < n
    for inc in rec["incidents"]:
        assert LEAD_IN <= inc["opened_i"] < n
        assert inc["closed_i"] is None or inc["closed_i"] >= inc["opened_i"]


def test_incident_targets_are_instances_or_tiers(rec):
    ids = {i["id"] for i in rec["instances"]}
    roles = {i["role"] for i in rec["instances"]}
    for inc in rec["incidents"]:
        assert inc["target"] in ids | roles


def test_recording_says_it_is_simulated(rec):
    assert rec["meta"]["simulated"] is True
    assert rec["truth"], "the truth lane is what lets the demo show missed faults"
