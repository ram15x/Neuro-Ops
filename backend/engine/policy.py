"""
Decides what to do about a diagnosis, and whether it is allowed right now.

The engine never acts just because a model fired. Every action has to pass
the guardrails below, and the default mode is shadow: decide, record, do
nothing.
"""
from dataclasses import dataclass, field
from datetime import timedelta

# cause -> ladder of actions to try in order
PLAYBOOK = {
    "disk_fill": ["disk_cleanup", "notify"],
    "memory_leak": ["restart_service", "reboot", "notify"],
    "runaway_process": ["restart_service", "reboot", "notify"],
    "traffic_surge": ["scale_out", "scale_out", "notify"],
    "cpu_contention": ["observe", "migrate_host", "notify"],
    "credit_exhaustion": ["enable_unlimited", "notify"],
    "network_loss": ["notify"],
    "memory_step": ["none"],
}

# what each action costs you if it is wrong
ACTIONS = {
    "disk_cleanup": dict(disruptive=False, needs_approval=False, reversible=False),
    "restart_service": dict(disruptive=True, needs_approval=False, reversible=False),
    "reboot": dict(disruptive=True, needs_approval=False, reversible=False),
    "scale_out": dict(disruptive=False, needs_approval=False, reversible=True),
    # stop and start means downtime, and unlimited credits cost money: a person decides
    "migrate_host": dict(disruptive=True, needs_approval=True, reversible=False),
    "enable_unlimited": dict(disruptive=False, needs_approval=True, reversible=True),
}


@dataclass
class PolicyConfig:
    mode: str = "shadow"                 # shadow | approval | auto
    cooldown: timedelta = timedelta(minutes=30)
    max_auto_actions_per_hour: int = 4
    protected_roles: tuple = ("db",)     # disruptive actions here always need a person
    observe_for: timedelta = timedelta(minutes=30)
    max_scale_out: int = 2


@dataclass
class PolicyState:
    last_action: dict = field(default_factory=dict)     # target -> time
    auto_times: list = field(default_factory=list)
    acting_in_tier: dict = field(default_factory=dict)  # role -> (instance, until)
    scaled: dict = field(default_factory=dict)          # role -> extra instances


def next_action(incident):
    """walk the ladder: skip anything already tried"""
    ladder = PLAYBOOK.get(incident.cause, ["notify"])
    used = dict(incident.attempts)
    for step in ladder:
        if used.get(step, 0) > 0:
            used[step] -= 1
            continue
        return step
    return "notify"


def decide(incident, now, instances, cfg, state):
    """returns (action, status, reasons)"""
    action = next_action(incident)
    reasons = []
    if action == "none":
        return action, "none", ["looks like a release, memory is higher but steady"]
    if action == "observe":
        if now - incident.opened < cfg.observe_for:
            return "observe", "none", [f"watching for {int(cfg.observe_for.total_seconds() // 60)} min before acting"]
        incident.attempts["observe"] = 1
        action = next_action(incident)
    if action == "notify":
        return action, "notify", ["no safe automatic fix left, a person needs to look"]

    spec = ACTIONS[action]
    role = {i.id: i.role for i in instances}
    tier = incident.target if incident.scope == "tier" else role.get(incident.target)
    peers = [i.id for i in instances if i.role == tier and i.id != incident.target]

    # guardrails that block outright
    last = state.last_action.get(incident.target)
    if last and now - last < cfg.cooldown:
        return action, "blocked", [f"cooldown: last action on this target was {int((now - last).total_seconds() // 60)} min ago"]
    if action == "scale_out" and state.scaled.get(tier, 0) >= cfg.max_scale_out:
        return action, "notify", [f"scale out cap reached (+{cfg.max_scale_out}), a person needs to look"]
    if spec["disruptive"]:
        busy = state.acting_in_tier.get(tier)
        if busy and busy[0] != incident.target and now < busy[1]:
            return action, "blocked", [f"another {tier} instance is being worked on, one at a time per tier"]

    if cfg.mode == "shadow":
        return action, "shadow", ["shadow mode: recorded, not executed"]

    # guardrails that hand the decision to a person
    if cfg.mode == "approval":
        reasons.append("approval mode")
    if spec["needs_approval"]:
        reasons.append("this action always needs a person (downtime or cost)")
    if spec["disruptive"] and tier in cfg.protected_roles:
        reasons.append(f"{tier} is a protected tier")
    if spec["disruptive"] and not peers:
        reasons.append("no other instance in this tier to carry traffic meanwhile")
    recent = [t for t in state.auto_times if now - t < timedelta(hours=1)]
    if len(recent) >= cfg.max_auto_actions_per_hour:
        reasons.append(f"rate limit: {len(recent)} automatic actions in the last hour")
    if reasons:
        return action, "needs_approval", reasons
    return action, "executed", ["all guardrails passed"]
