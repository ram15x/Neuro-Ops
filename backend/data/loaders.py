"""
One shape for every data source, so detectors and the scorecard don't care
where a series came from.
"""
import json
import os
from dataclasses import dataclass, field

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NAB_DIR = os.path.join(ROOT, "datasets", "inframind")
NAB_LABELS = os.path.join(NAB_DIR, "nab_windows.json")
REAL_EC2 = os.path.join(ROOT, "data", "real_training_data.csv")

# group metrics so thresholds are tuned per kind (a cpu % and a byte count
# can't share one static threshold)
KIND = {
    "CPUUtilization": "cpu",
    "mem_used_percent": "memory",
    "disk_used_percent": "disk",
    "NetworkIn": "network",
    "CPUCreditBalance": "credits",
}


@dataclass
class Series:
    name: str
    source: str            # nab | sim | real
    kind: str              # cpu | memory | disk | network | credits
    frame: pd.DataFrame    # timestamp, value
    windows: list = field(default_factory=list)   # (start, end) of real problems
    benign: list = field(default_factory=list)    # (start, end) of harmless changes
    simulated: bool = False


def load_nab():
    """Labelled CPU series from the Numenta Anomaly Benchmark (real AWS data from 2014)."""
    labels = json.load(open(NAB_LABELS))
    out = []
    for fname, wins in sorted(labels.items()):
        path = os.path.join(NAB_DIR, os.path.basename(fname))
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=["timestamp"])
        w = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in wins]
        out.append(Series(os.path.basename(fname)[:-4], "nab", "cpu", df, w))
    return out


def load_sim(days=14, seed=7, density=2):
    """Series from the simulated fleet, with the injected faults as labels."""
    from backend.sim.aws_fleet import simulate
    res = simulate(days=days, seed=seed, density=density)
    out = []
    for (iid, metric), g in res.metrics.groupby(["instance_id", "metric"], sort=True):
        wins, benign = [], []
        for e in res.events:
            if e.instance_id != iid or metric not in e.metrics:
                continue
            # credits fall slowly during a drain, that is a forecast job, not an anomaly
            if metric == "CPUCreditBalance":
                continue
            # 30 minutes of grace after the end, recovery is part of the incident
            span = (pd.Timestamp(e.start), pd.Timestamp(e.end) + pd.Timedelta(minutes=30))
            (benign if e.benign else wins).append(span)
        if metric == "CPUCreditBalance":
            continue
        out.append(Series(f"sim{seed}/{iid}/{metric}", "sim", KIND[metric],
                          g[["timestamp", "value"]].reset_index(drop=True), wins, benign, simulated=True))
    return out


def load_real_ec2():
    """
    The 2.3 days collected from the original EC2 instance in March 2026.
    No trustworthy labels here: the old is_anomaly column was just cpu > 80.
    The stress-test runs (cpu held above 80) are used as known load events.
    """
    df = pd.read_csv(REAL_EC2, parse_dates=["timestamp"])
    df = df.dropna(subset=["cpu"]).sort_values("timestamp")
    frame = df[["timestamp", "cpu"]].rename(columns={"cpu": "value"}).reset_index(drop=True)
    hot = frame["value"] > 80
    wins, start, prev = [], None, None
    for t, h in zip(frame["timestamp"], hot):
        if h and start is None:
            start = t
        if not h and start is not None:
            wins.append((start, prev)); start = None
        prev = t
    if start is not None:
        wins.append((start, prev))
    # runs a few minutes apart are one stress test
    merged = []
    for a, b in wins:
        if merged and (a - merged[-1][1]) <= pd.Timedelta(minutes=10):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [Series("real_ec2_cpu", "real", "cpu", frame, merged)]
