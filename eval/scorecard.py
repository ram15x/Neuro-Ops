"""
Detector scorecard.

Scores every detector on labelled data and writes docs/scorecard.md.

How it is kept fair:
- every detector only sees the past
- alerts within an hour of each other count as one episode
- all detectors are compared at the same false-alarm budget
- the alert threshold is picked on one half of the series and scored on the
  other half, then the halves swap, so no detector is graded on data it was
  tuned on

Run:  python -m eval.scorecard
"""
import json
import os
import sys

import numpy as np
import pandas as pd

from backend.data.loaders import load_nab, load_sim, load_real_ec2, ROOT
from backend.detect.detectors import DETECTORS, prepare
from backend.detect.forecast import time_to_breach

WARM = 0.20          # first fifth of each series is learning time, never scored
MERGE_STEPS = 12     # one hour at 5-minute data
BUDGETS = (0.1, 0.25, 0.5)   # false episodes allowed per series per day
MAIN_BUDGET = 0.25


def _episodes(alert):
    idx = np.flatnonzero(alert)
    if not len(idx):
        return []
    starts = [idx[0]]
    for prev, cur in zip(idx[:-1], idx[1:]):
        if cur - prev > MERGE_STEPS:
            starts.append(cur)
    return starts


def _prepare(series_list, detectors):
    """score once per detector, everything after this is just thresholding"""
    prepared = []
    for s in series_list:
        ts = pd.DatetimeIndex(s.frame["timestamp"])
        values = prepare(s.frame["value"].to_numpy(), s.kind)
        n_warm = int(len(values) * WARM)
        scores = {name: DETECTORS[name](values, ts, n_warm) for name in detectors}
        in_fault = np.zeros(len(ts), bool)
        in_benign = np.zeros(len(ts), bool)
        for a, b in s.windows:
            in_fault |= (ts >= a) & (ts <= b)
        for a, b in s.benign:
            in_benign |= (ts >= a) & (ts <= b)
        wins = [(a, b) for a, b in s.windows if b >= ts[n_warm]]
        days = (ts[-1] - ts[n_warm]).total_seconds() / 86400
        prepared.append(dict(s=s, ts=ts, n_warm=n_warm, scores=scores, in_fault=in_fault,
                             in_benign=in_benign, wins=wins, days=days))
    return prepared


def _count(p, name, thr):
    alert = p["scores"][name] > thr
    alert[:p["n_warm"]] = False
    hits, leads = 0, []
    for a, b in p["wins"]:
        inside = np.flatnonzero(alert & (p["ts"] >= a) & (p["ts"] <= b))
        if len(inside):
            hits += 1
            leads.append((p["ts"][inside[0]] - a).total_seconds() / 60)
    false = benign = 0
    for i in _episodes(alert):
        if p["in_fault"][i]:
            continue
        if p["in_benign"][i]:
            benign += 1
        else:
            false += 1
    return hits, false, benign, leads


def _pick_threshold(group, name, budget):
    pooled = np.concatenate([p["scores"][name][p["n_warm"]:] for p in group])
    pooled = pooled[np.isfinite(pooled)]
    best_thr, best_hits = np.inf, -1
    days = sum(p["days"] for p in group)
    for thr in np.unique(np.quantile(pooled, np.linspace(0.70, 0.9999, 150)))[::-1]:
        hits = false = 0
        for p in group:
            h, f, _, _ = _count(p, name, thr)
            hits += h; false += f
        if false / days > budget:
            break   # thresholds only get looser from here
        if hits > best_hits:
            best_hits, best_thr = hits, thr
    return best_thr


def evaluate(series_list, budgets=BUDGETS, detectors=None):
    detectors = list(detectors or DETECTORS)
    prepared = _prepare(series_list, detectors)
    # two folds per metric kind, alternating so both halves look alike
    folds = {}
    for kind in sorted({p["s"].kind for p in prepared}):
        members = [p for p in prepared if p["s"].kind == kind]
        folds[kind] = (members[0::2], members[1::2])
    out = {}
    for budget in budgets:
        for name in detectors:
            tot = dict(hits=0, windows=0, false=0, benign=0, days=0.0, leads=[], by_kind={})
            for kind, (fa, fb) in folds.items():
                k = dict(hits=0, windows=0)
                for tune, test in ((fa, fb), (fb, fa)):
                    if not tune or not test:
                        continue
                    thr = _pick_threshold(tune, name, budget)
                    for p in test:
                        h, f, b, leads = _count(p, name, thr)
                        tot["hits"] += h; tot["false"] += f; tot["benign"] += b
                        tot["windows"] += len(p["wins"]); tot["days"] += p["days"]; tot["leads"] += leads
                        k["hits"] += h; k["windows"] += len(p["wins"])
                tot["by_kind"][kind] = k
            out[(budget, name)] = dict(
                recall=tot["hits"] / max(tot["windows"], 1), hits=tot["hits"], windows=tot["windows"],
                false_per_day=tot["false"] / max(tot["days"], 1e-9), benign_flagged=tot["benign"],
                median_lead_min=float(np.median(tot["leads"])) if tot["leads"] else None,
                by_kind=tot["by_kind"],
            )
    return out


def evaluate_forecast(seeds=(7, 8, 9), horizon_steps=72):
    """how good is 'this will breach in N hours' on simulated leaks, fills and credit drains"""
    from backend.sim.aws_fleet import simulate
    targets = {"memory_leak": ("mem_used_percent", 90, True),
               "disk_fill": ("disk_used_percent", 90, True),
               "credit_drain": ("CPUCreditBalance", 5, False)}
    errors = {k: [] for k in targets}
    warned = {k: [0, 0] for k in targets}      # [events warned before breach, events]
    lead = {k: [] for k in targets}
    false_warn, quiet_days = 0, 0.0
    for seed in seeds:
        res = simulate(seed=seed)
        for kind, (metric, limit, rising) in targets.items():
            for iid in res.metrics.instance_id.unique():
                s = res.series(iid, metric)
                if s.empty:
                    continue
                v = s["value"].to_numpy(); ts = pd.DatetimeIndex(s["timestamp"])
                evs = [e for e in res.events if e.instance_id == iid and e.kind == kind and e.breach_at]
                busy = np.zeros(len(ts), bool)
                for e in res.events:
                    if e.instance_id == iid and metric in e.metrics:
                        busy |= (ts >= pd.Timestamp(e.start) - pd.Timedelta(hours=1)) & (ts <= pd.Timestamp(e.end) + pd.Timedelta(hours=6))
                # quiet time: any warning here is a false one
                last = -10 ** 9
                for i in range(288 * 3, len(ts), 6):
                    if busy[i]:
                        continue
                    if time_to_breach(v[:i + 1], limit, rising, horizon=horizon_steps) and i - last > 72:
                        false_warn += 1; last = i
                quiet_days += (~busy[288 * 3:]).sum() / 288
                for e in evs:
                    a = ts.get_loc(pd.Timestamp(e.start)); br = ts.get_loc(pd.Timestamp(e.breach_at))
                    warned[kind][1] += 1
                    first = None
                    for i in range(a, br, 3):
                        b = time_to_breach(v[:i + 1], limit, rising, horizon=horizon_steps)
                        if b:
                            first = first if first is not None else i
                            errors[kind].append(abs(b.eta_steps - (br - i)) * 5)
                    if first is not None:
                        warned[kind][0] += 1
                        lead[kind].append((br - first) * 5)
    return dict(
        kinds={k: dict(events=warned[k][1], warned=warned[k][0],
                       median_eta_error_min=round(float(np.median(errors[k])), 1) if errors[k] else None,
                       median_warning_lead_min=float(np.median(lead[k])) if lead[k] else None) for k in targets},
        false_warnings_per_series_day=false_warn / max(quiet_days, 1e-9), false_warnings=false_warn,
        quiet_series_days=quiet_days,
    )


def _table(res, budget):
    rows = sorted([(n, r) for (b, n), r in res.items() if b == budget], key=lambda t: (-t[1]["recall"], t[1]["false_per_day"]))
    lines = ["| Detector | Caught | Recall | False alarms / series-day | Median minutes from window start to first alert |", "|---|---|---|---|---|"]
    for n, r in rows:
        lead = "-" if r["median_lead_min"] is None else f"{r['median_lead_min']:.0f}"
        lines.append(f"| {n} | {r['hits']}/{r['windows']} | {r['recall']:.2f} | {r['false_per_day']:.2f} | {lead} |")
    return "\n".join(lines)


def _kind_table(res, budget):
    kinds = sorted({k for (b, n), r in res.items() for k in r["by_kind"]})
    lines = ["| Detector | " + " | ".join(kinds) + " |", "|---|" + "---|" * len(kinds)]
    for (b, n), r in res.items():
        if b != budget:
            continue
        cells = [f"{r['by_kind'][k]['hits']}/{r['by_kind'][k]['windows']}" if k in r["by_kind"] else "-" for k in kinds]
        lines.append(f"| {n} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def run(write=True):
    nab = evaluate(load_nab())
    sim_series = load_sim(seed=7) + load_sim(seed=8) + load_sim(seed=9)
    sim = evaluate(sim_series)
    fc = evaluate_forecast()
    from eval import closed_loop
    loop_clean = closed_loop.evaluate()
    loop_hard = closed_loop.evaluate(hard=True)
    summary = {
        "closed_loop": {"clean": loop_clean, "hard": loop_hard},
        "nab": {f"{b}|{n}": {k: v for k, v in r.items()} for (b, n), r in nab.items()},
        "sim": {f"{b}|{n}": {k: v for k, v in r.items()} for (b, n), r in sim.items()},
        "forecast": fc,
    }
    if write:
        os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
        json.dump(summary, open(os.path.join(ROOT, "eval", "scorecard.json"), "w"), indent=1)
        md = ["# Detector scorecard", "",
              "Generated by `python -m eval.scorecard`. Do not edit by hand.", "",
              "Rules: detectors only see the past, alerts within an hour are one episode, "
              "thresholds are picked on half the series and scored on the other half.", ""]
        for budget in BUDGETS:
            md += [f"## NAB, real AWS CPU series (budget {budget} false alarms per series-day)", "", _table(nab, budget), ""]
        for budget in BUDGETS:
            md += [f"## Simulated fleet, 3 seeds (budget {budget})", "",
                   "SIMULATED data: faults are injected by `backend/sim/aws_fleet.py`.", "", _table(sim, budget), ""]
        md += [f"### Simulated fleet by metric kind (budget {MAIN_BUDGET})", "", _kind_table(sim, MAIN_BUDGET), ""]
        md += ["Slow ramps (disk, memory) are the weak spot of anomaly detectors, which is why the "
               "forecast below exists.", "",
               "The real EC2 capture in `data/real_training_data.csv` is not scored: it holds about "
               "14 hours of points across 2.3 days, less than one learning window.", ""]
        md += ["## Time-to-breach forecast (simulated)", "",
               "| Fault | Events | Warned before breach | Median warning lead (min) | Median ETA error (min) |", "|---|---|---|---|---|"]
        for k, r in fc["kinds"].items():
            md.append(f"| {k} | {r['events']} | {r['warned']} | {r['median_warning_lead_min']} | {r['median_eta_error_min']} |")
        md += ["", f"False warnings on quiet time: {fc['false_warnings']} over {fc['quiet_series_days']:.0f} series-days "
                   f"({fc['false_warnings_per_series_day']:.3f} per series-day).", ""]
        md += ["## Closed loop on the simulated fleet", "",
               "SIMULATED, seeds 21 to 23, which were not used to calibrate thresholds. The simulator decides "
               "which action fixes which fault, so this checks the engine's logic (diagnosis, choice of action, "
               "guardrails, verification), not whether these fixes work on real machines. Shadow mode executes "
               "nothing, so it doubles as the no-healing baseline.", "",
               closed_loop.markdown(loop_clean, "Clean faults"),
               "The clean numbers are close to perfect because the faults are well separated and the "
               "diagnosis rules were written by the same hand as the simulator. Hard mode is the more "
               "useful read.", "",
               closed_loop.markdown(loop_hard, "Hard mode: twice the noise, weaker and overlapping faults, harmless cpu bursts")]
        open(os.path.join(ROOT, "docs", "scorecard.md"), "w").write("\n".join(md))
    return summary


# CI fails if the default detector or the forecast drops below these.
# they sit a little under the current numbers so normal noise passes.
DEFAULT_DETECTOR = "hybrid_seasonal"
FLOORS = {"nab": 0.40, "sim": 0.75}
MAX_FALSE_FORECASTS = 0.01   # per series-day


def check(summary):
    problems = []
    for part, floor in FLOORS.items():
        r = summary[part][f"{MAIN_BUDGET}|{DEFAULT_DETECTOR}"]
        if r["recall"] < floor:
            problems.append(f"{part}: {DEFAULT_DETECTOR} recall {r['recall']:.2f} is under the floor {floor}")
    fc = summary["forecast"]
    for kind, r in fc["kinds"].items():
        if r["warned"] < r["events"]:
            problems.append(f"forecast: only warned on {r['warned']}/{r['events']} {kind} events")
    if fc["false_warnings_per_series_day"] > MAX_FALSE_FORECASTS:
        problems.append(f"forecast: {fc['false_warnings_per_series_day']:.3f} false warnings per series-day")
    loop = summary.get("closed_loop")
    if loop:
        clean, shadow = loop["clean"]["auto"], loop["clean"]["shadow"]
        if shadow["actions"]:
            problems.append("closed loop: shadow mode executed something")
        if clean["wrong_actions"]:
            problems.append(f"closed loop: {clean['wrong_actions']} actions with no fault behind them on clean data")
        if clean["breaches"] > 0.2 * clean["breaches_possible"]:
            problems.append(f"closed loop: {clean['breaches']}/{clean['breaches_possible']} ramps still breached")
        if clean["diagnosed"] < 0.9 * clean["faults"]:
            problems.append(f"closed loop: only {clean['diagnosed']}/{clean['faults']} faults diagnosed correctly")
    return problems


if __name__ == "__main__":
    s = run()
    if "--check" in sys.argv:
        bad = check(s)
        for b in bad:
            print("FAIL", b)
        if bad:
            sys.exit(1)
        print("scorecard floors ok")
    for part in ("nab", "sim"):
        print(f"\n{part} @ {MAIN_BUDGET}")
        for k, r in sorted(s[part].items(), key=lambda t: -t[1]["recall"]):
            if k.startswith(f"{MAIN_BUDGET}|"):
                print(f"  {k.split('|')[1]:18s} {r['hits']}/{r['windows']}  fp/day {r['false_per_day']:.2f}  benign {r['benign_flagged']}")
    print("\nforecast", json.dumps(s["forecast"], indent=1))
