"""
One shape for "what the dashboard shows", used by the live API and by the
recordings that power the hosted demo.
"""
import numpy as np
import pandas as pd

from backend.engine.model import as_dict

DECIMALS = {"CPUUtilization": 1, "mem_used_percent": 1, "disk_used_percent": 1, "CPUCreditBalance": 0}


def snapshot(fleet, engine, lo, hi, scenario="clean", truth=True):
    """everything between step lo (inclusive) and hi (exclusive), indexed from lo"""
    ts = fleet.ts
    t0 = ts[lo]

    def idx(when):
        if when is None:
            return None
        return int((pd.Timestamp(when) - t0) / pd.Timedelta(minutes=5))

    metrics = {}
    for iid in fleet.instances:
        data = fleet.render(iid)
        metrics[iid] = {}
        for m, v in data.items():
            if m == "NetworkIn":
                # megabytes per 5 minutes reads better than raw bytes
                metrics[iid][m] = np.round(v[lo:hi] / 1e6, 3).tolist()
            else:
                metrics[iid][m] = np.round(v[lo:hi], DECIMALS[m]).tolist()

    events = []
    for a in engine.audit:
        i = idx(a["ts"])
        if i is None or i < 0 or i >= hi - lo:
            continue
        events.append({**{k: v for k, v in a.items() if k not in ("ts", "simulated")}, "i": i})

    out = {
        "meta": {"simulated": bool(engine.source.simulated), "scenario": scenario, "mode": engine.cfg.mode,
                 "start": t0.isoformat(), "step_minutes": 5, "steps": hi - lo, "seed": fleet.seed},
        "instances": [{"id": i.id, "role": i.role, "type": i.itype} for i in engine.instances],
        "metrics": metrics,
        "incidents": [{**{k: v for k, v in as_dict(inc).items() if k in ("id", "cause", "scope", "target", "severity", "evidence", "logs")},
                       "opened_i": idx(inc.opened), "closed_i": idx(inc.closed)} for inc in engine.incidents
                      if idx(inc.opened) < hi - lo and (inc.closed is None or idx(inc.closed) >= 0)],
        "audit": events,
        "approvals": [{"id": a["id"], "incident": a["incident"], "action": a["action"], "target": a["target"],
                       "reasons": a["reasons"], "status": a["status"], "i": idx(a["requested"])} for a in engine.approvals],
    }
    if truth:
        # the faults the simulator injected. a real system never has this lane.
        out["truth"] = [{"kind": e.kind, "instance_id": e.instance_id, "benign": e.benign,
                         "start_i": idx(e.start), "end_i": idx(e.end), "breach_i": idx(e.breach_at),
                         "stopped_i": idx(e.stopped_at), "metrics": e.metrics} for e in fleet.events]
    return out
