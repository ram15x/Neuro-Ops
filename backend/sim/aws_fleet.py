"""
Simulated AWS fleet.

We have no AWS account to pull from, so this builds CloudWatch-shaped metrics
for a small fleet and injects faults at known times. Because we place the
faults ourselves, the labels are exact. Everything from here is SIMULATED and
is marked that way wherever it is shown.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

PERIOD_MIN = 5  # CloudWatch basic monitoring gives one datapoint per 5 minutes
STEPS_PER_DAY = 24 * 60 // PERIOD_MIN

# namespace per metric, same split as real AWS: the hypervisor sees cpu, network
# and credits, the CloudWatch agent inside the box reports memory and disk
NAMESPACE = {
    "CPUUtilization": "AWS/EC2",
    "NetworkIn": "AWS/EC2",
    "CPUCreditBalance": "AWS/EC2",
    "mem_used_percent": "CWAgent",
    "disk_used_percent": "CWAgent",
}

# role -> (cpu base, cpu swing with load, memory base, disk start, disk growth per day)
# burstable boxes have to average under their baseline (20% per vcpu here) or
# they run out of credits, so the web and api tiers idle low like real t3 fleets
ROLES = {
    "web": dict(cpu=6, swing=14, mem=42, disk=30, disk_growth=0.25, net=4e6),
    "api": dict(cpu=8, swing=14, mem=55, disk=35, disk_growth=0.30, net=2e6),
    "db": dict(cpu=22, swing=10, mem=70, disk=52, disk_growth=0.50, net=1e6),
    "batch": dict(cpu=4, swing=2, mem=30, disk=40, disk_growth=0.20, net=2e5),
}

FLEET = [
    ("i-0a1web0000000001", "web", "t3.small"),
    ("i-0a1web0000000002", "web", "t3.small"),
    ("i-0a1web0000000003", "web", "t3.small"),
    ("i-0b2api0000000001", "api", "t3.medium"),
    ("i-0b2api0000000002", "api", "t3.medium"),
    ("i-0c3db00000000001", "db", "m5.large"),
    ("i-0d4batch00000001", "batch", "t3.small"),
]

# t3 burst credits: earned per hour, max bank, vcpus
T3 = {"t3.small": (24, 576, 2), "t3.medium": (24, 576, 2)}  # both have a 20% per-vcpu baseline

# which metrics each fault shows up in
FAULT_METRICS = {
    "cpu_runaway": ["CPUUtilization"],
    "noisy_neighbour": ["CPUUtilization"],
    "memory_leak": ["mem_used_percent"],
    "disk_fill": ["disk_used_percent"],
    "traffic_surge": ["CPUUtilization", "NetworkIn"],
    "network_drop": ["NetworkIn"],
    "credit_drain": ["CPUUtilization", "CPUCreditBalance"],
    "deploy_step": ["mem_used_percent"],  # benign, a new release that just uses more memory
}


@dataclass
class Event:
    kind: str
    instance_id: str
    start: datetime
    end: datetime
    metrics: list
    benign: bool = False
    # for ramps: when the metric actually crosses its danger line
    breach_at: datetime = None

    def to_dict(self):
        d = asdict(self)
        for k in ("start", "end", "breach_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class SimResult:
    metrics: pd.DataFrame  # timestamp, instance_id, role, namespace, metric, value
    events: list = field(default_factory=list)
    simulated: bool = True

    def series(self, instance_id, metric):
        m = self.metrics
        s = m[(m.instance_id == instance_id) & (m.metric == metric)]
        return s[["timestamp", "value"]].reset_index(drop=True)


def _load_curve(ts, rng):
    # traffic shape: busy in the afternoon, quiet at night, lighter on weekends
    hour = np.asarray(ts.hour + ts.minute / 60, dtype=float)
    daily = 0.5 + 0.5 * np.sin((hour - 8) / 24 * 2 * np.pi)
    weekly = np.where(np.asarray(ts.dayofweek) >= 5, 0.6, 1.0)
    # slow wandering noise so no two days look identical
    noise = np.zeros(len(ts))
    for i in range(1, len(ts)):
        noise[i] = 0.97 * noise[i - 1] + rng.normal(0, 0.02)
    return np.clip(daily * weekly + noise, 0, 1.6)


def _schedule(rng, n_steps, warm_steps, fleet, density):
    """pick fault times so that two faults never overlap on one instance"""
    plan = []
    busy = {iid: [] for iid, _, _ in fleet}

    def free(iid, a, b):
        return all(b < x or a > y for x, y in busy[iid])

    def place(kind, iid, dur):
        for _ in range(50):
            a = int(rng.integers(warm_steps, n_steps - dur - 24))
            if free(iid, a - 36, a + dur + 36):
                busy[iid].append((a - 36, a + dur + 36))
                plan.append((kind, iid, a, a + dur))
                return True
        return False

    by_role = {}
    for iid, role, _ in fleet:
        by_role.setdefault(role, []).append(iid)
    everyone = [iid for iid, _, _ in fleet]
    serving = by_role.get("web", []) + by_role.get("api", [])

    for _ in range(density):
        place("cpu_runaway", rng.choice(everyone), int(rng.integers(6, 24)))
        place("noisy_neighbour", rng.choice(serving), int(rng.integers(12, 36)))
        place("memory_leak", rng.choice(serving + by_role.get("db", [])), int(rng.integers(72, 216)))
        place("disk_fill", rng.choice(everyone), int(rng.integers(48, 144)))
        place("network_drop", rng.choice(serving), int(rng.integers(4, 12)))
        place("deploy_step", rng.choice(serving), 6)
        place("credit_drain", rng.choice(by_role.get("web", everyone)), int(rng.integers(96, 144)))
        # a surge hits the whole web tier at once
        dur = int(rng.integers(12, 24))
        a = int(rng.integers(warm_steps, n_steps - dur - 24))
        web = by_role.get("web", [])
        if web and all(free(i, a - 36, a + dur + 36) for i in web):
            for i in web:
                busy[i].append((a - 36, a + dur + 36))
                plan.append(("traffic_surge", i, a, a + dur))
    return plan


def simulate(days=14, seed=7, start="2026-03-02", warm_days=3, density=2, fleet=None):
    """Build `days` of metrics for the fleet. Same seed gives the same data."""
    fleet = fleet or FLEET
    rng = np.random.default_rng(seed)
    n = days * STEPS_PER_DAY
    ts = pd.date_range(start, periods=n, freq=f"{PERIOD_MIN}min")
    plan = _schedule(rng, n, warm_days * STEPS_PER_DAY, fleet, density)
    shared_load = _load_curve(ts, rng)

    rows, events = [], []
    for iid, role, itype in fleet:
        p = ROLES[role]
        # each box follows the shared traffic with a bit of its own noise
        load = np.clip(shared_load + rng.normal(0, 0.03, n), 0, 2.5)
        cpu = p["cpu"] + p["swing"] * load + rng.normal(0, 1.5, n)
        if role == "batch":
            # nightly job at 02:00 for an hour, normal and expected
            night = np.asarray(ts.hour) == 2
            cpu = cpu + np.where(night, 78 + rng.normal(0, 3, n), 0)
        mem = p["mem"] + 4 * load + np.cumsum(rng.normal(0, 0.05, n)) * 0.3 + rng.normal(0, 0.6, n)
        disk = p["disk"] + p["disk_growth"] * np.arange(n) / STEPS_PER_DAY + rng.normal(0, 0.05, n)
        net = p["net"] * (0.15 + load) * rng.lognormal(0, 0.12, n)

        for kind, who, a, b in plan:
            if who != iid:
                continue
            ev = Event(kind, iid, ts[a].to_pydatetime(), ts[b].to_pydatetime(), FAULT_METRICS[kind])
            L = b - a
            if kind == "cpu_runaway":
                cpu[a:b] = rng.uniform(88, 99) + rng.normal(0, 1.5, L)
            elif kind == "noisy_neighbour":
                cpu[a:b] += rng.uniform(14, 24) + rng.normal(0, 5, L)
            elif kind == "traffic_surge":
                boost = rng.uniform(1.0, 1.5)
                cpu[a:b] += p["swing"] * boost
                net[a:b] *= 1 + boost
            elif kind == "network_drop":
                net[a:b] *= rng.uniform(0.01, 0.06)
            elif kind == "memory_leak":
                top = rng.uniform(93, 98)
                # real leaks are rarely a clean line: some speed up, and a
                # garbage collector gives the climb a sawtooth
                power = rng.choice([1.0, 1.0, 1.6, 2.2])
                ramp = np.linspace(0, 1, L) ** power * (top - mem[a])
                saw = rng.uniform(0, 4) * ((np.arange(L) % 9) / 9 - 0.5)
                mem[a:b] += ramp + saw
                # the process gets killed at the top and memory falls back
                over = np.flatnonzero(mem[a:b] >= 90)
                if len(over):
                    ev.breach_at = ts[a + over[0]].to_pydatetime()
            elif kind == "disk_fill":
                top = rng.uniform(94, 99)
                # logs get written in bursts, so the fill is a staircase more than a line
                bursts = rng.gamma(0.6, 1.0, L)
                ramp = np.cumsum(bursts) / bursts.sum() * (top - disk[a])
                disk[a:b] += ramp
                # someone cleans up at the end, so it drops back
                over = np.flatnonzero(disk[a:b] >= 90)
                if len(over):
                    ev.breach_at = ts[a + over[0]].to_pydatetime()
            elif kind == "deploy_step":
                mem[a:] += rng.uniform(7, 11)
                ev.benign = True
            elif kind == "credit_drain":
                cpu[a:b] = rng.uniform(72, 85) + rng.normal(0, 2, L)
            events.append(ev)

        cpu = np.clip(cpu, 0, 100)
        # burst credits: earn a fixed amount, spend by cpu use, throttle when empty
        bal = None
        if itype in T3:
            earn_h, cap, vcpu = T3[itype]
            baseline = earn_h / 60 / vcpu * 100
            bal = np.empty(n)
            b_now = cap * 0.8
            for i in range(n):
                spend = cpu[i] / 100 * vcpu * PERIOD_MIN
                b_now = min(cap, b_now + earn_h * PERIOD_MIN / 60 - spend)
                if b_now <= 0:
                    b_now = 0
                    cpu[i] = min(cpu[i], baseline + rng.normal(0, 0.5))
                bal[i] = b_now
        for ev in events:
            if bal is not None and ev.instance_id == iid and ev.kind == "credit_drain":
                a = ts.get_loc(pd.Timestamp(ev.start)); b = ts.get_loc(pd.Timestamp(ev.end))
                low = np.flatnonzero(bal[a:b] <= 5)
                if len(low):
                    ev.breach_at = ts[a + low[0]].to_pydatetime()

        out = {
            "CPUUtilization": cpu,
            "NetworkIn": net,
            "mem_used_percent": np.clip(mem, 0, 100),
            "disk_used_percent": np.clip(disk, 0, 100),
        }
        if bal is not None:
            # fixed-size instances like m5 have no credit metric at all
            out["CPUCreditBalance"] = bal
        for metric, values in out.items():
            rows.append(pd.DataFrame({
                "timestamp": ts, "instance_id": iid, "role": role,
                "namespace": NAMESPACE[metric], "metric": metric, "value": np.round(values, 3),
            }))

    return SimResult(pd.concat(rows, ignore_index=True), sorted(events, key=lambda e: e.start))
