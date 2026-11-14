"""
Simulated application and system log lines for the fleet.

SIMULATED, and written by the same person who wrote the faults, so a test that
asks "do the fault lines show up as the loudest new cluster" is checking the
mechanics, not whether real logs behave this way. Real logs are what
eval/logs_eval.py is for. What is realistic here: ordinary traffic noise with
its own harmless warnings and errors, ids and numbers that change on every
line, and fault lines that start quietly and get louder as the fault grows.
"""
import numpy as np
import pandas as pd

HOSTS = ["10.0.1.14", "10.0.2.31", "10.0.2.77", "10.0.3.9"]
TABLES = ["orders", "users", "sessions", "invoices"]
JOBS = ["rollup", "cleanup", "export"]


def _hex(rng, n=12):
    return "".join(rng.choice(list("0123456789abcdef"), n))


def _normal(role, rng):
    """what a healthy box says, including harmless warnings that come and go"""
    out = []
    if role == "web":
        for _ in range(rng.poisson(5)):
            out.append(rng.choice([
                f"GET /api/orders/{rng.integers(1000, 99999)} 200 {rng.integers(8, 90)}ms",
                f"GET /static/app.{_hex(rng, 8)}.js 304 {rng.integers(1, 6)}ms",
                f"POST /login 200 {rng.integers(20, 140)}ms user={rng.integers(100, 9999)}",
                f"GET /health 200 {rng.integers(1, 4)}ms"], p=[.4, .3, .1, .2]))
    elif role == "api":
        for _ in range(rng.poisson(5)):
            out.append(rng.choice([
                f"query ok table={rng.choice(TABLES)} rows={rng.integers(1, 400)} {rng.integers(2, 60)}ms",
                f"cache hit key=user:{rng.integers(100, 9999)}",
                f"handled request {_hex(rng)} in {rng.integers(10, 120)}ms"], p=[.4, .3, .3]))
    elif role == "db":
        for _ in range(rng.poisson(3)):
            out.append(rng.choice([
                f"connection received host={rng.choice(HOSTS)} port={rng.integers(30000, 60000)}",
                f"checkpoint complete: wrote {rng.integers(50, 900)} buffers ({rng.random() * 3:.1f}%)",
                f"automatic vacuum of table {rng.choice(TABLES)}: removed {rng.integers(0, 3000)} rows"], p=[.5, .25, .25]))
    else:
        if rng.random() < .3:
            out.append(f"job {rng.choice(JOBS)} heartbeat run={_hex(rng, 8)}")
    # harmless noise every real system has
    if rng.random() < .06:
        out.append(f"WARN slow query {rng.integers(400, 900)}ms table={rng.choice(TABLES)}")
    if rng.random() < .04:
        out.append(f"ERROR client disconnected before response request_id={_hex(rng)}")
    if rng.random() < .03:
        out.append(f"WARN retrying upstream attempt 2 host={rng.choice(HOSTS)}")
    return out


def _fault_lines(kind, p, rng):
    """p runs 0 to 1 over the life of the fault, lines get louder and nastier as it grows"""
    out = []
    if kind == "memory_leak":
        if rng.random() < .3 + .7 * p:
            out.append(f"WARN GC overhead high: {int(20 + 60 * p)}% of time spent in garbage collection")
        if p > .6 and rng.random() < p:
            out.append(f"ERROR OutOfMemoryError: unable to allocate {rng.integers(1, 64) * 1048576} bytes")
        if p > .8 and rng.random() < .5:
            out.append(f"WARN worker {rng.integers(1000, 9999)} killed by oom-killer")
    elif kind == "disk_fill":
        if rng.random() < .3 + .5 * p:
            out.append(f"WARN large log file app-{rng.integers(1, 40)}.log size={rng.integers(200, 4000)}MB")
        if p > .5 and rng.random() < .5:
            out.append(f"WARN log rotation skipped: disk usage {int(80 + 18 * p)}%")
        if p > .7 and rng.random() < p:
            out.append(f"ERROR write failed: No space left on device path=/var/log/app/app-{rng.integers(1, 40)}.log")
    elif kind in ("cpu_runaway", "credit_drain"):
        if rng.random() < .8:
            out.append(f"ERROR worker pid={rng.integers(1000, 9999)} spinning at {rng.integers(90, 100)}% cpu, no progress for {rng.integers(20, 600)}s")
        if rng.random() < .5:
            out.append(f"WARN request queue depth {rng.integers(20, 400)}")
        if kind == "credit_drain" and rng.random() < .4 + .5 * p:
            out.append(f"WARN cpu throttled: burst credit balance low, {int(100 * (1 - p))} credits left")
    elif kind == "noisy_neighbour":
        if rng.random() < .7:
            out.append(f"WARN scheduling delay {rng.integers(40, 400)}ms, cpu steal {rng.integers(8, 30)}%")
    elif kind == "traffic_surge":
        if rng.random() < .8:
            out.append("WARN upstream request queue full, shedding load")
        if rng.random() < .5:
            out.append(f"ERROR 503 Service Unavailable upstream=app request_id={_hex(rng)}")
        if rng.random() < .4:
            out.append(f"INFO requests per second {rng.integers(900, 3000)} above target 600")
    elif kind == "network_drop":
        if rng.random() < .8:
            out.append(f"ERROR connection reset by peer host={rng.choice(HOSTS)}:{rng.integers(30000, 60000)}")
        if rng.random() < .4:
            out.append(f"ERROR no route to host {rng.choice(HOSTS)}")
        if rng.random() < .5:
            out.append("WARN health check failed: timeout after 5s")
    return out


def fleet_logs(fleet, iid, lo, hi):
    """(timestamp, line) for steps lo up to but not including hi"""
    role = fleet.instances[iid]["role"]
    idx = list(fleet.instances).index(iid)
    hard = getattr(fleet, "hard", False)
    events = [e for e in fleet.events if e.instance_id == iid]
    out = []
    for step in range(max(0, lo), min(hi, fleet.n)):
        ts = fleet.ts[step]
        rng = np.random.default_rng([fleet.seed, idx, step])
        lines = _normal(role, rng)
        for e in events:
            a, b = pd.Timestamp(e.start), pd.Timestamp(e.stopped_at or e.end)
            if not a <= ts < b:
                continue
            if e.benign:
                if ts == a and e.kind == "deploy_step":
                    lines += [f"INFO deploying release v1.{rng.integers(2, 9)}.{rng.integers(0, 20)}", "INFO service restarted",
                              f"WARN cache cold, warming (hit rate {rng.integers(5, 40)}%)"]
                elif ts == a and e.kind == "cron_burst":
                    lines.append(f"INFO cron job {rng.choice(JOBS)} started")
                continue
            prog = (ts - a) / (pd.Timestamp(e.end) - a)
            fl = _fault_lines(e.kind, float(prog), rng)
            if hard:
                # in hard mode the fault is quieter, and some of its lines are dropped
                fl = [x for x in fl if rng.random() < .5]
            lines += fl
        for k, line in enumerate(lines):
            out.append((ts + pd.Timedelta(seconds=int(rng.integers(0, 299)) if k else 0), str(line)))
    out.sort(key=lambda x: x[0])
    return out
