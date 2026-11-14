"""
Closed-loop evaluation on the simulated fleet.

Runs the whole engine twice per seed: once in shadow mode (nothing is
executed, so this is what happens with no healing) and once in auto mode with
a stand-in operator who approves requests after 15 minutes.

Read the numbers for what they are: the simulator decides which action fixes
which fault, so this checks the engine's logic (does it diagnose correctly,
pick the matching action, respect guardrails, verify), not whether these
fixes work on real machines.
"""
import pandas as pd

from backend.engine.engine import Engine
from backend.engine.executors import SimExecutor
from backend.engine.policy import PolicyConfig
from backend.engine.sources import SimSource
from backend.sim.aws_fleet import build_fleet

# which diagnosis counts as right for each injected fault
EXPECTED = {
    "cpu_runaway": {"runaway_process"},
    "credit_drain": {"runaway_process", "credit_exhaustion"},
    "noisy_neighbour": {"cpu_contention"},
    "memory_leak": {"memory_leak"},
    "disk_fill": {"disk_fill"},
    "traffic_surge": {"traffic_surge"},
    "network_drop": {"network_loss"},
    "deploy_step": {"memory_step"},
    "cron_burst": set(),
}


def _run(seed, mode, hard=False):
    fleet = build_fleet(seed=seed, hard=hard)
    eng = Engine(SimSource(fleet), SimExecutor(fleet), PolicyConfig(mode=mode), use_logs=False,
                 auto_approve_after=3 if mode == "auto" else None).run()
    return fleet, eng


def _score(fleet, eng):
    role = {iid: i["role"] for iid, i in fleet.instances.items()}
    out = dict(faults=0, detected=0, diagnosed=0, benign=0, benign_left_alone=0, breaches_possible=0,
               breaches=0, fault_minutes=0.0, actions=0, wrong_actions=0, verified=0, verify_failed=0,
               approvals=0, escalated=0, by_kind={})
    actions = [a for a in eng.audit if a["type"] == "action" and a["ok"]]
    for e in fleet.events:
        start, end = pd.Timestamp(e.start), pd.Timestamp(e.end)
        seen = [i for i in eng.incidents
                if (i.target == e.instance_id or i.target == role[e.instance_id])
                and start <= pd.Timestamp(i.opened) <= end + pd.Timedelta(minutes=30)]
        if e.benign:
            out["benign"] += 1
            acted = [a for a in actions if a["target"] == e.instance_id
                     and start <= pd.Timestamp(a["ts"]) <= end + pd.Timedelta(hours=2)]
            out["benign_left_alone"] += 0 if acted else 1
            continue
        k = out["by_kind"].setdefault(e.kind, dict(faults=0, detected=0, diagnosed=0))
        out["faults"] += 1; k["faults"] += 1
        if seen:
            out["detected"] += 1; k["detected"] += 1
        if any(i.cause in EXPECTED[e.kind] for i in seen):
            out["diagnosed"] += 1; k["diagnosed"] += 1
        stop = pd.Timestamp(e.stopped_at) if e.stopped_at else end
        out["fault_minutes"] += (min(stop, end) - start).total_seconds() / 60
        if e.breach_at:
            out["breaches_possible"] += 1
            if stop > pd.Timestamp(e.breach_at):
                out["breaches"] += 1
    for a in actions:
        out["actions"] += 1
        ts = pd.Timestamp(a["ts"])
        members = fleet.tier(a["target"]) or [a["target"]]
        real = [e for e in fleet.events if not e.benign and e.instance_id in members
                and pd.Timestamp(e.start) <= ts <= pd.Timestamp(e.end) + pd.Timedelta(minutes=30)]
        if not real:
            out["wrong_actions"] += 1
    for a in eng.audit:
        if a["type"] == "verified": out["verified"] += 1
        if a["type"] == "verify_failed": out["verify_failed"] += 1
        if a["type"] == "approved": out["approvals"] += 1
        if a["type"] == "escalated": out["escalated"] += 1
    return out


def _add(total, part):
    for k, v in part.items():
        if k == "by_kind":
            for kind, d in v.items():
                t = total.setdefault("by_kind", {}).setdefault(kind, dict(faults=0, detected=0, diagnosed=0))
                for kk in d:
                    t[kk] += d[kk]
        else:
            total[k] = total.get(k, 0) + v
    return total


def evaluate(seeds=(21, 22, 23), hard=False):
    res = {"shadow": {}, "auto": {}}
    for seed in seeds:
        for mode in res:
            fleet, eng = _run(seed, mode, hard)
            _add(res[mode], _score(fleet, eng))
    return res


def markdown(res, title="Closed loop on the simulated fleet"):
    s, a = res["shadow"], res["auto"]
    lines = [f"### {title}", "",
             "| | Shadow (no healing) | Auto, operator approves after 15 min |", "|---|---|---|",
             f"| Faults injected | {s['faults']} | {a['faults']} |",
             f"| Faults noticed | {s['detected']} | {a['detected']} |",
             f"| Diagnosed as the right cause | {s['diagnosed']} | {a['diagnosed']} |",
             f"| Ramps that reached the danger line | {s['breaches']}/{s['breaches_possible']} | {a['breaches']}/{a['breaches_possible']} |",
             f"| Total fault time (hours) | {s['fault_minutes'] / 60:.0f} | {a['fault_minutes'] / 60:.0f} |",
             f"| Actions executed | {s['actions']} | {a['actions']} |",
             f"| Actions with no real fault behind them | {s['wrong_actions']} | {a['wrong_actions']} |",
             f"| Actions verified / not verified | - | {a['verified']} / {a['verify_failed']} |",
             f"| Approvals asked for | - | {a['approvals']} |",
             f"| Handed to a person | {s['escalated']} | {a['escalated']} |",
             f"| Harmless events left alone | {s['benign_left_alone']}/{s['benign']} | {a['benign_left_alone']}/{a['benign']} |", "",
             "Diagnosis by fault type (auto mode):", "",
             "| Fault | Injected | Noticed | Right cause |", "|---|---|---|---|"]
    for kind, d in sorted(a["by_kind"].items()):
        lines.append(f"| {kind} | {d['faults']} | {d['detected']} | {d['diagnosed']} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(markdown(evaluate(), "Clean faults"))
    print(markdown(evaluate(hard=True), "Hard mode"))
