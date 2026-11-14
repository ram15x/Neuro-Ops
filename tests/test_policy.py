from datetime import datetime, timedelta

from backend.engine.model import Incident, Instance
from backend.engine.policy import PolicyConfig, PolicyState, decide, next_action

NOW = datetime(2026, 3, 10, 12, 0)
FLEET = [Instance("web-1", "web", "t3.small"), Instance("web-2", "web", "t3.small"),
         Instance("db-1", "db", "m5.large")]


def inc(cause, target, scope="instance", opened=NOW, attempts=None):
    return Incident(1, cause, scope, target, opened, "critical", attempts=attempts or {})


def test_shadow_mode_never_executes():
    action, status, _ = decide(inc("disk_fill", "web-1"), NOW, FLEET, PolicyConfig(mode="shadow"), PolicyState())
    assert (action, status) == ("disk_cleanup", "shadow")


def test_auto_mode_runs_a_low_risk_fix():
    action, status, _ = decide(inc("disk_fill", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert (action, status) == ("disk_cleanup", "executed")


def test_approval_mode_asks_for_everything():
    _, status, reasons = decide(inc("disk_fill", "web-1"), NOW, FLEET, PolicyConfig(mode="approval"), PolicyState())
    assert status == "needs_approval" and "approval mode" in reasons


def test_cooldown_blocks_a_second_action():
    state = PolicyState(last_action={"web-1": NOW - timedelta(minutes=10)})
    _, status, reasons = decide(inc("memory_leak", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), state)
    assert status == "blocked" and "cooldown" in reasons[0]


def test_cooldown_ends():
    state = PolicyState(last_action={"web-1": NOW - timedelta(minutes=31)})
    _, status, _ = decide(inc("memory_leak", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), state)
    assert status == "executed"


def test_protected_tier_needs_a_person_for_a_restart():
    _, status, reasons = decide(inc("memory_leak", "db-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert status == "needs_approval"
    assert any("protected" in r for r in reasons)
    assert any("no other instance" in r for r in reasons)


def test_protected_tier_still_gets_a_harmless_cleanup():
    _, status, _ = decide(inc("disk_fill", "db-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert status == "executed"


def test_one_disruptive_action_per_tier_at_a_time():
    state = PolicyState(acting_in_tier={"web": ("web-2", NOW + timedelta(minutes=10))})
    _, status, reasons = decide(inc("memory_leak", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), state)
    assert status == "blocked" and "one at a time" in reasons[0]


def test_rate_limit_hands_over_to_a_person():
    state = PolicyState(auto_times=[NOW - timedelta(minutes=m) for m in (5, 10, 15, 20)])
    _, status, reasons = decide(inc("disk_fill", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), state)
    assert status == "needs_approval" and any("rate limit" in r for r in reasons)


def test_downtime_and_cost_actions_always_ask():
    i = inc("cpu_contention", "web-1", opened=NOW - timedelta(hours=1))
    action, status, _ = decide(i, NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert (action, status) == ("migrate_host", "needs_approval")
    action, status, _ = decide(inc("credit_exhaustion", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert (action, status) == ("enable_unlimited", "needs_approval")


def test_contention_is_watched_before_anything_happens():
    action, status, _ = decide(inc("cpu_contention", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert (action, status) == ("observe", "none")


def test_a_release_gets_no_action():
    action, status, _ = decide(inc("memory_step", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert (action, status) == ("none", "none")


def test_network_loss_goes_to_a_person():
    action, status, _ = decide(inc("network_loss", "web-1"), NOW, FLEET, PolicyConfig(mode="auto"), PolicyState())
    assert (action, status) == ("notify", "notify")


def test_ladder_escalates_after_failed_attempts():
    assert next_action(inc("memory_leak", "web-1")) == "restart_service"
    assert next_action(inc("memory_leak", "web-1", attempts={"restart_service": 1})) == "reboot"
    assert next_action(inc("memory_leak", "web-1", attempts={"restart_service": 1, "reboot": 1})) == "notify"
    assert next_action(inc("traffic_surge", "web", "tier", attempts={"scale_out": 1})) == "scale_out"
    assert next_action(inc("traffic_surge", "web", "tier", attempts={"scale_out": 2})) == "notify"


def test_scale_out_is_capped():
    state = PolicyState(scaled={"web": 2})
    _, status, reasons = decide(inc("traffic_surge", "web", "tier"), NOW, FLEET, PolicyConfig(mode="auto"), state)
    assert status == "notify" and "cap" in reasons[0]
