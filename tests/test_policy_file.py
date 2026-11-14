from datetime import timedelta

import pytest
import yaml

from backend.engine.policy import PLAYBOOK
from backend.engine.policy_file import PolicyError, load, parse

DEFAULT = "config/policy.yaml"


def test_the_shipped_file_matches_the_built_in_defaults():
    cfg = load(DEFAULT)
    assert cfg.playbook == PLAYBOOK
    assert cfg.mode == "shadow" and cfg.cooldown == timedelta(minutes=30) and cfg.protected_roles == ("db",)


def test_changes_are_applied():
    cfg = parse({"mode": "approval", "guardrails": {"cooldown_minutes": 10, "protected_tiers": ["db", "batch"]},
                 "always_ask": ["reboot"], "playbook": {"disk_fill": ["disk_cleanup", "reboot", "notify"]}})
    assert cfg.mode == "approval" and cfg.cooldown == timedelta(minutes=10)
    assert cfg.protected_roles == ("db", "batch") and cfg.extra_approval == ("reboot",)
    assert cfg.playbook["disk_fill"] == ["disk_cleanup", "reboot", "notify"]
    assert cfg.playbook["memory_leak"] == PLAYBOOK["memory_leak"]   # untouched causes keep their ladder


@pytest.mark.parametrize("doc,fragment", [
    ({"mode": "yolo"}, "mode must be"),
    ({"typo": 1}, "unknown top-level key"),
    ({"guardrails": {"cooldown": 5}}, "unknown guardrail"),
    ({"guardrails": {"max_scale_out": -1}}, "whole number"),
    ({"guardrails": {"max_scale_out": True}}, "whole number"),
    ({"always_ask": ["notify"]}, "not an action that can be run"),
    ({"playbook": {"disk_full": ["notify"]}}, "unknown cause"),
    ({"playbook": {"disk_fill": ["defrag", "notify"]}}, "unknown action"),
    ({"playbook": {"disk_fill": ["disk_cleanup"]}}, "has to end with notify"),
    ({"playbook": {"disk_fill": []}}, "non-empty"),
    ("just a string", "mapping"),
])
def test_bad_files_are_refused_with_a_reason(doc, fragment):
    with pytest.raises(PolicyError, match=fragment):
        parse(doc)


def test_an_extra_approval_really_holds_the_action_back():
    import pandas as pd
    from backend.engine import policy as P
    from backend.engine.model import Incident, Instance
    inst = [Instance("w1", "web", "t3.small"), Instance("w2", "web", "t3.small")]
    now = pd.Timestamp("2026-03-10 12:00").to_pydatetime()
    inc = Incident(1, "memory_leak", "instance", "w1", now - timedelta(hours=1), "critical")
    assert P.decide(inc, now, inst, P.PolicyConfig(mode="auto"), P.PolicyState())[1] == "executed"
    cfg = parse({"mode": "auto", "always_ask": ["restart_service"]})
    assert P.decide(inc, now, inst, cfg, P.PolicyState())[1] == "needs_approval"


def test_a_playbook_change_changes_what_the_engine_does_next():
    import pandas as pd
    from backend.engine import policy as P
    from backend.engine.model import Incident, Instance
    inst = [Instance("w1", "web", "t3.small"), Instance("w2", "web", "t3.small")]
    now = pd.Timestamp("2026-03-10 12:00").to_pydatetime()
    inc = Incident(1, "disk_fill", "instance", "w1", now, "critical")
    cfg = parse({"mode": "auto", "playbook": {"disk_fill": ["notify"]}})
    assert P.decide(inc, now, inst, cfg, P.PolicyState())[1] == "notify"


def test_yaml_roundtrip_of_the_shipped_file_is_valid_yaml():
    assert isinstance(yaml.safe_load(open(DEFAULT)), dict)
