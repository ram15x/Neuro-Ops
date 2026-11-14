"""
Simulated AWS fleet.

We have no AWS account to pull from, so this builds CloudWatch-shaped metrics
for a small fleet and injects faults at known times. Because we place the
faults ourselves, the labels are exact. Everything from here is SIMULATED and
is marked that way wherever it is shown.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime

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
    "cron_burst": ["CPUUtilization"],     # benign, a short scheduled job (hard mode only)
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
    # set when a healing action cut the fault short
    stopped_at: datetime = None

    def to_dict(self):
        d = asdict(self)
        for k in ("start", "end", "breach_at", "stopped_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class SimResult:
    metrics: pd.DataFrame  # timestamp, instance_id, role, namespace, metric, value
    events: list = field(default_factory=list)
    simulated: bool = True
    fleet: object = None

    def series(self, instance_id, metric):
        m = self.metrics
        s = m[(m.instance_id == instance_id) & (m.metric == metric)]
        return s[["timestamp", "value"]].reset_index(drop=True)


class Fleet:
    """
    The fleet as something you can act on. Each instance has its normal
    behaviour plus one layer per fault, so a healing action can switch a fault
    off from a given moment and the metrics after that change, the way they
    would if the action had really worked.
    """

    def __init__(self, ts, seed):
        self.ts = ts
        self.n = len(ts)
        self.seed = seed
        self.instances = {}     # id -> dict(role, itype, base={metric: array}, unlimited_from=None)
        self.events = []
        self._layers = []       # (event or None, instance_id, metric, start, array, mode)
        self._cache = {}
        self.capacity = {}      # role -> extra instances added by scale out, as (step, count)

    # ---- building ----
    def add_instance(self, iid, role, itype, base):
        self.instances[iid] = dict(role=role, itype=itype, base=base, unlimited_from=None)

    def add_layer(self, event, iid, metric, start, array, mode="add"):
        # mode "add" adds to the metric, "mul" multiplies it
        self._layers.append([event, iid, metric, start, np.asarray(array, dtype=float), mode])
        self._cache.pop(iid, None)

    # ---- reading ----
    def render(self, iid):
        if iid in self._cache:
            return self._cache[iid]
        inst = self.instances[iid]
        out = {m: v.copy() for m, v in inst["base"].items()}
        for event, who, metric, start, arr, mode in self._layers:
            if who != iid or mode != "add":
                continue
            out[metric][start:start + len(arr)] += arr
        for event, who, metric, start, arr, mode in self._layers:
            if who != iid or mode != "mul":
                continue
            out[metric][start:start + len(arr)] *= arr
        cpu = np.clip(out["CPUUtilization"], 0, 100)
        if inst["itype"] in T3:
            earn_h, cap, vcpu = T3[inst["itype"]]
            baseline = earn_h / 60 / vcpu * 100
            # fixed noise per instance so re-rendering gives the same numbers
            jitter = np.random.default_rng(self.seed * 1000 + list(self.instances).index(iid)).normal(0, 0.5, self.n)
            bal = np.empty(self.n)
            b_now = cap * 0.8
            free_from = inst["unlimited_from"]
            for i in range(self.n):
                spend = cpu[i] / 100 * vcpu * PERIOD_MIN
                b_now = min(cap, b_now + earn_h * PERIOD_MIN / 60 - spend)
                if b_now <= 0:
                    b_now = 0
                    # out of credits: throttled to baseline, unless unlimited mode is on
                    if free_from is None or i < free_from:
                        cpu[i] = min(cpu[i], baseline + jitter[i])
                bal[i] = b_now
            out["CPUCreditBalance"] = bal
        out["CPUUtilization"] = cpu
        out["NetworkIn"] = np.clip(out["NetworkIn"], 0, None)
        for m in ("mem_used_percent", "disk_used_percent"):
            out[m] = np.clip(out[m], 0, 100)
        self._cache[iid] = out
        return out

    def frame(self):
        rows = []
        for iid, inst in self.instances.items():
            for metric, values in self.render(iid).items():
                rows.append(pd.DataFrame({
                    "timestamp": self.ts, "instance_id": iid, "role": inst["role"],
                    "namespace": NAMESPACE[metric], "metric": metric, "value": np.round(values, 3),
                }))
        return pd.concat(rows, ignore_index=True)

    def active(self, iid, step, kinds=None):
        ts = self.ts[step]
        return [e for e in self.events
                if e.instance_id == iid and pd.Timestamp(e.start) <= ts < pd.Timestamp(e.stopped_at or e.end)
                and (kinds is None or e.kind in kinds)]

    def tier(self, role):
        return [iid for iid, inst in self.instances.items() if inst["role"] == role]

    # ---- acting ----
    def _stop(self, event, step):
        for layer in self._layers:
            if layer[0] is event:
                start, arr = layer[3], layer[4]
                cut = max(0, step - start)
                if cut < len(arr):
                    layer[4] = arr[:cut]
        event.stopped_at = self.ts[step].to_pydatetime()
        self._cache.pop(event.instance_id, None)

    def _downtime(self, iid, step, steps):
        # the box is gone for a moment: no cpu, no traffic
        end = min(self.n, step + steps)
        cur = self.render(iid)
        self.add_layer(None, iid, "CPUUtilization", step, -cur["CPUUtilization"][step:end])
        self.add_layer(None, iid, "NetworkIn", step, np.zeros(end - step), mode="mul")

    def restart_service(self, iid, step):
        # assumption baked into the simulator: leaks, runaway processes and
        # stuck hot workers all go away when the service restarts
        fixed = self.active(iid, step, ("memory_leak", "cpu_runaway", "credit_drain"))
        for e in fixed:
            self._stop(e, step)
        return [e.kind for e in fixed]

    def reboot(self, iid, step):
        fixed = self.active(iid, step, ("memory_leak", "cpu_runaway", "credit_drain"))
        for e in fixed:
            self._stop(e, step)
        self._downtime(iid, step, 1)
        return [e.kind for e in fixed]

    def disk_cleanup(self, iid, step):
        fixed = self.active(iid, step, ("disk_fill",))
        for e in fixed:
            self._stop(e, step)
        return [e.kind for e in fixed]

    def migrate_host(self, iid, step):
        # stop and start lands the instance on different hardware
        fixed = self.active(iid, step, ("noisy_neighbour", "memory_leak", "cpu_runaway", "credit_drain"))
        for e in fixed:
            self._stop(e, step)
        self._downtime(iid, step, 2)
        return [e.kind for e in fixed]

    def scale(self, role, step, delta):
        # more boxes share the same traffic, so each one carries less of a surge
        members = self.tier(role)
        before = len(members) + sum(c for _, c in self.capacity.get(role, []))
        after = before + delta
        if after < len(members):
            return []
        self.capacity.setdefault(role, []).append((step, delta))
        touched = []
        for iid in members:
            for e in self.active(iid, step, ("traffic_surge",)):
                for layer in list(self._layers):
                    if layer[0] is e and layer[5] == "add":
                        start, arr = layer[3], layer[4]
                        cut = max(0, step - start)
                        arr = arr.copy(); arr[cut:] *= before / after
                        layer[4] = arr
                touched.append(iid)
            self._cache.pop(iid, None)
        return touched

    def set_unlimited(self, iid, step, on=True):
        self.instances[iid]["unlimited_from"] = step if on else None
        self._cache.pop(iid, None)
        return self.active(iid, step, ("credit_drain",)) != []


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


def _schedule(rng, n_steps, warm_steps, fleet, density, hard=False):
    """pick fault times so that two faults never overlap on one instance"""
    plan = []
    busy = {iid: [] for iid, _, _ in fleet}

    def free(iid, a, b):
        # hard mode lets faults pile up on one instance
        return hard or all(b < x or a > y for x, y in busy[iid])

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
        if hard:
            for _ in range(2):
                place("cron_burst", rng.choice(serving), int(rng.integers(2, 4)))
        # a surge hits the whole web tier at once
        dur = int(rng.integers(12, 24))
        a = int(rng.integers(warm_steps, n_steps - dur - 24))
        web = by_role.get("web", [])
        if web and all(free(i, a - 36, a + dur + 36) for i in web):
            for i in web:
                busy[i].append((a - 36, a + dur + 36))
                plan.append(("traffic_surge", i, a, a + dur))
    return plan


def build_fleet(days=14, seed=7, start="2026-03-02", warm_days=3, density=2, fleet=None, hard=False):
    """
    hard=True is a stress setting: twice the noise, faults about 60% as strong,
    faults allowed to overlap on one instance, and short harmless cpu bursts
    thrown in. It exists to find where detection and diagnosis break.
    """
    fleet = fleet or FLEET
    noise, size = (2.0, 0.6) if hard else (1.0, 1.0)
    rng = np.random.default_rng(seed)
    n = days * STEPS_PER_DAY
    ts = pd.date_range(start, periods=n, freq=f"{PERIOD_MIN}min")
    plan = _schedule(rng, n, warm_days * STEPS_PER_DAY, fleet, density, hard)
    shared_load = _load_curve(ts, rng)
    F = Fleet(ts, seed)
    F.hard = hard

    for iid, role, itype in fleet:
        p = ROLES[role]
        # each box follows the shared traffic with a bit of its own noise
        load = np.clip(shared_load + rng.normal(0, 0.03, n), 0, 2.5)
        cpu = p["cpu"] + p["swing"] * load + rng.normal(0, 1.5 * noise, n)
        if role == "batch":
            # nightly job at 02:00 for an hour, normal and expected
            night = np.asarray(ts.hour) == 2
            cpu = cpu + np.where(night, 78 + rng.normal(0, 3, n), 0)
        mem = p["mem"] + 4 * load + np.cumsum(rng.normal(0, 0.05, n)) * 0.3 + rng.normal(0, 0.6 * noise, n)
        disk = p["disk"] + p["disk_growth"] * np.arange(n) / STEPS_PER_DAY + rng.normal(0, 0.05, n)
        net = p["net"] * (0.15 + load) * rng.lognormal(0, 0.12 * noise, n)
        F.add_instance(iid, role, itype, {
            "CPUUtilization": cpu, "NetworkIn": net, "mem_used_percent": mem, "disk_used_percent": disk})

        for kind, who, a, b in plan:
            if who != iid:
                continue
            ev = Event(kind, iid, ts[a].to_pydatetime(), ts[b].to_pydatetime(), FAULT_METRICS[kind])
            L = b - a
            if kind == "cpu_runaway":
                F.add_layer(ev, iid, "CPUUtilization", a, (rng.uniform(88, 99) + rng.normal(0, 1.5, L) - cpu[a:b]) * size)
            elif kind == "noisy_neighbour":
                F.add_layer(ev, iid, "CPUUtilization", a, (rng.uniform(14, 24) + rng.normal(0, 5, L)) * size)
            elif kind == "traffic_surge":
                boost = rng.uniform(1.0, 1.5) * size
                F.add_layer(ev, iid, "CPUUtilization", a, np.full(L, p["swing"] * boost))
                F.add_layer(ev, iid, "NetworkIn", a, net[a:b] * boost)
            elif kind == "network_drop":
                F.add_layer(ev, iid, "NetworkIn", a, np.full(L, rng.uniform(0.01, 0.06) if not hard else rng.uniform(0.2, 0.5)), mode="mul")
            elif kind == "memory_leak":
                top = rng.uniform(93, 98)
                # real leaks are rarely a clean line: some speed up, and a
                # garbage collector gives the climb a sawtooth
                power = rng.choice([1.0, 1.0, 1.6, 2.2])
                ramp = np.linspace(0, 1, L) ** power * (top - mem[a])
                saw = rng.uniform(0, 4) * ((np.arange(L) % 9) / 9 - 0.5)
                F.add_layer(ev, iid, "mem_used_percent", a, ramp + saw)
            elif kind == "disk_fill":
                top = rng.uniform(94, 99)
                # logs get written in bursts, so the fill is a staircase more than a line
                bursts = rng.gamma(0.6, 1.0, L)
                F.add_layer(ev, iid, "disk_used_percent", a, np.cumsum(bursts) / bursts.sum() * (top - disk[a]))
            elif kind == "deploy_step":
                F.add_layer(ev, iid, "mem_used_percent", a, np.full(n - a, rng.uniform(7, 11)))
                ev.benign = True
            elif kind == "cron_burst":
                F.add_layer(ev, iid, "CPUUtilization", a, np.full(L, rng.uniform(35, 50)))
                ev.benign = True
            elif kind == "credit_drain":
                F.add_layer(ev, iid, "CPUUtilization", a, rng.uniform(72, 85) + rng.normal(0, 2, L) - cpu[a:b])
            F.events.append(ev)

    # note when each ramp crosses its danger line if nobody steps in
    limits = {"memory_leak": ("mem_used_percent", lambda v: v >= 90),
              "disk_fill": ("disk_used_percent", lambda v: v >= 90),
              "credit_drain": ("CPUCreditBalance", lambda v: v <= 5)}
    for ev in F.events:
        if ev.kind in limits:
            metric, crossed = limits[ev.kind]
            v = F.render(ev.instance_id).get(metric)
            if v is None:
                continue
            a, b = ts.get_loc(pd.Timestamp(ev.start)), ts.get_loc(pd.Timestamp(ev.end))
            hit = np.flatnonzero(crossed(v[a:b]))
            if len(hit):
                ev.breach_at = ts[a + hit[0]].to_pydatetime()
    F.events.sort(key=lambda e: e.start)
    return F


def simulate(days=14, seed=7, start="2026-03-02", warm_days=3, density=2, fleet=None, hard=False):
    """Build `days` of metrics for the fleet. Same seed gives the same data."""
    F = build_fleet(days, seed, start, warm_days, density, fleet, hard)
    return SimResult(F.frame(), F.events, fleet=F)
