"""
Prometheus text format for the engine itself, so Grafana or Alertmanager can
watch it. Written by hand: this is a dozen counters, not worth a dependency.
"""
from collections import Counter


def _line(name, value, **labels):
    lab = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in sorted(labels.items()))
    return f"{name}{{{lab}}} {value}" if lab else f"{name} {value}"


def render(engine, step, last_tick_seconds, ticks):
    audit = engine.audit
    out = []

    def metric(name, kind, help_text, rows):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(rows)

    metric("neuroops_incidents_total", "counter", "Incidents opened, by cause.",
           [_line("neuroops_incidents_total", n, cause=c) for c, n in sorted(Counter(i.cause for i in engine.incidents).items())]
           or [_line("neuroops_incidents_total", 0, cause="none")])
    metric("neuroops_incidents_open", "gauge", "Incidents open right now.", [_line("neuroops_incidents_open", len(engine.open))])
    acts = Counter((a["action"], "true" if a["ok"] else "false") for a in audit if a["type"] == "action")
    metric("neuroops_actions_total", "counter", "Actions the engine ran, by action and whether the call worked.",
           [_line("neuroops_actions_total", n, action=a, ok=ok) for (a, ok), n in sorted(acts.items())]
           or [_line("neuroops_actions_total", 0, action="none", ok="true")])
    metric("neuroops_verifications_total", "counter", "Checks after an action, by result.",
           [_line("neuroops_verifications_total", sum(a["type"] == t for a in audit), result=r)
            for t, r in (("verified", "worked"), ("verify_failed", "failed"))])
    notes = Counter((a.get("why"), "true" if a.get("ok") else "false") for a in audit if a["type"] == "notified")
    metric("neuroops_alerts_total", "counter", "Alerts, by kind and whether they went out.",
           [_line("neuroops_alerts_total", n, kind=k, ok=ok) for (k, ok), n in sorted(notes.items(), key=str)]
           or [_line("neuroops_alerts_total", 0, kind="none", ok="true")])
    metric("neuroops_pending_approvals", "gauge", "Actions waiting for a person.",
           [_line("neuroops_pending_approvals", sum(a["status"] == "pending" for a in engine.approvals))])
    metric("neuroops_mode", "gauge", "1 for the engine's current mode.",
           [_line("neuroops_mode", int(engine.cfg.mode == m), mode=m) for m in ("shadow", "approval", "auto")])
    metric("neuroops_simulated", "gauge", "1 when the data is simulated.", [_line("neuroops_simulated", int(engine.source.simulated))])
    metric("neuroops_engine_step", "gauge", "Position in the run (5 minute steps).", [_line("neuroops_engine_step", step)])
    metric("neuroops_ticks_total", "counter", "Engine ticks so far.", [_line("neuroops_ticks_total", ticks)])
    metric("neuroops_tick_seconds", "gauge", "How long the last tick took.", [_line("neuroops_tick_seconds", round(last_tick_seconds, 4))])
    return "\n".join(out) + "\n"
