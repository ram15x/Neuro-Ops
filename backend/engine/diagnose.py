"""
Works out the likely cause from the signals that are active right now.

This replaces the old "service correlation", which made up four services.
Here the correlation is across real signals: several metrics on one instance,
and the same metric across the instances of one tier.
"""
from collections import defaultdict

from backend.engine.model import Diagnosis

SEV = {"warning": 1, "critical": 2}


def _worst(signals):
    return max(signals, key=lambda s: SEV[s.severity]).severity


def diagnose(signals, instances):
    """signals: confirmed signals for this tick. instances: list of Instance."""
    role = {i.id: i.role for i in instances}
    tier_size = defaultdict(int)
    for i in instances:
        tier_size[i.role] += 1
    by = defaultdict(list)
    for s in signals:
        by[(s.instance_id, s.metric)].append(s)

    def has(iid, metric, kind=None, direction=None):
        return [s for s in by.get((iid, metric), [])
                if (kind is None or s.kind == kind) and (direction is None or s.direction == direction)]

    out = []
    # cpu up on most of a tier at once, with traffic up too, is load and not a broken box
    surge_tiers = set()
    cpu_up = defaultdict(list)
    net_up = defaultdict(list)
    for i in instances:
        if has(i.id, "CPUUtilization", "anomaly", "up") or has(i.id, "CPUUtilization", "limit"):
            cpu_up[i.role].append(i.id)
        if has(i.id, "NetworkIn", "anomaly", "up"):
            net_up[i.role].append(i.id)
    for r, ids in cpu_up.items():
        # two boxes in one tier breaking the same way in the same minutes is
        # unlikely, shared load is the simpler story even before traffic confirms it
        if tier_size[r] >= 2 and len(ids) >= 2 and len(ids) * 2 >= tier_size[r]:
            surge_tiers.add(r)
            sigs = [s for iid in ids for s in by[(iid, "CPUUtilization")]]
            note = "traffic is up too" if net_up.get(r) else "traffic signal not confirmed yet"
            out.append(Diagnosis("traffic_surge", "tier", r, _worst(sigs),
                                 [f"cpu up on {len(ids)}/{tier_size[r]} {r} instances at once, {note}"]
                                 + [s.detail for s in sigs][:3]))

    for i in instances:
        iid = i.id
        disk = has(iid, "disk_used_percent", "forecast") + has(iid, "disk_used_percent", "limit")
        if disk:
            out.append(Diagnosis("disk_fill", "instance", iid, _worst(disk), [s.detail for s in disk]))

        mem_fc = has(iid, "mem_used_percent", "forecast")
        mem_lim = has(iid, "mem_used_percent", "limit")
        mem_an = has(iid, "mem_used_percent", "anomaly")
        if mem_fc or mem_lim:
            sigs = mem_fc + mem_lim
            out.append(Diagnosis("memory_leak", "instance", iid, _worst(sigs), [s.detail for s in sigs]))
        elif mem_an:
            # memory jumped but is not climbing: usually a release, nothing to heal
            out.append(Diagnosis("memory_step", "instance", iid, "warning", [s.detail for s in mem_an]))

        credit = has(iid, "CPUCreditBalance", "forecast") + has(iid, "CPUCreditBalance", "limit")
        cpu = has(iid, "CPUUtilization", "anomaly", "up") + has(iid, "CPUUtilization", "limit")
        if credit:
            out.append(Diagnosis("credit_exhaustion", "instance", iid, _worst(credit), [s.detail for s in credit]))
        if cpu and role[iid] not in surge_tiers:
            peak = max(s.value for s in cpu)
            if peak >= 60:
                out.append(Diagnosis("runaway_process", "instance", iid, "critical",
                                     [s.detail for s in cpu] + ["peers in the tier are normal, so this is one box and not load"]))
            else:
                out.append(Diagnosis("cpu_contention", "instance", iid, "warning",
                                     [s.detail for s in cpu] + ["cpu raised without matching traffic"]))

        drop = has(iid, "NetworkIn", "anomaly", "down")
        if drop:
            out.append(Diagnosis("network_loss", "instance", iid, _worst(drop), [s.detail for s in drop]))
    return out
