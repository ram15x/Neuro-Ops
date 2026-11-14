"""
Where metrics come from. The engine only talks to this small interface, so
the simulator, a recorded capture and real CloudWatch are interchangeable.
"""
from datetime import timedelta

import numpy as np
import pandas as pd

from backend.engine.model import Instance

METRICS = ["CPUUtilization", "NetworkIn", "mem_used_percent", "disk_used_percent", "CPUCreditBalance"]


class SimSource:
    """Reads from the simulated fleet. `simulated` is carried through to every output."""
    simulated = True
    replayable = True   # the whole run exists up front, so scores can be computed once

    def __init__(self, fleet):
        self.fleet = fleet
        self.ts = fleet.ts

    def instances(self):
        return [Instance(iid, i["role"], i["itype"]) for iid, i in self.fleet.instances.items()]

    def metrics(self, iid):
        return list(self.fleet.render(iid).keys())

    def full(self, iid, metric):
        return self.fleet.render(iid)[metric]

    def history(self, iid, metric, step):
        return self.ts[:step + 1], self.fleet.render(iid)[metric][:step + 1]

    def logs(self, iid, start, end):
        """simulated log lines between two times, see backend/sim/logs.py"""
        from backend.sim.logs import fleet_logs
        lo = int(self.ts.searchsorted(pd.Timestamp(start)))
        hi = int(self.ts.searchsorted(pd.Timestamp(end)))
        return [(t, l) for t, l in fleet_logs(self.fleet, iid, lo, hi) if pd.Timestamp(start) <= t < pd.Timestamp(end)]


class CloudWatchSource:
    """
    Real AWS. Never run against a live account in this project: the calls are
    checked against moto (a local fake of AWS), which proves the request
    shapes but not IAM, timing or real instance behaviour.
    """
    simulated = False
    replayable = False

    NAMESPACE = {"CPUUtilization": "AWS/EC2", "NetworkIn": "AWS/EC2", "CPUCreditBalance": "AWS/EC2",
                 "mem_used_percent": "CWAgent", "disk_used_percent": "CWAgent"}

    def __init__(self, region, lookback_days=14, session=None, log_group=None):
        import boto3
        s = session or boto3.session.Session(region_name=region)
        self.logs_client = s.client("logs", region_name=region)
        self.log_group = log_group   # one group, one stream per instance id (the CloudWatch agent default)
        self.ec2 = s.client("ec2", region_name=region)
        self.cw = s.client("cloudwatch", region_name=region)
        self.lookback = timedelta(days=lookback_days)

    def instances(self):
        out = []
        pages = self.ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
        for page in pages:
            for res in page["Reservations"]:
                for i in res["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    # the Role tag groups instances into tiers, untagged ones stand alone
                    out.append(Instance(i["InstanceId"], tags.get("Role", i["InstanceId"]), i["InstanceType"]))
        return out

    def metrics(self, iid):
        return list(METRICS)

    def history_at(self, iid, metric, now):
        r = self.cw.get_metric_statistics(
            Namespace=self.NAMESPACE[metric], MetricName=metric,
            Dimensions=[{"Name": "InstanceId", "Value": iid}],
            StartTime=now - self.lookback, EndTime=now, Period=300, Statistics=["Average"])
        pts = sorted(r["Datapoints"], key=lambda d: d["Timestamp"])
        ts = pd.DatetimeIndex([d["Timestamp"] for d in pts]).tz_localize(None) if pts else pd.DatetimeIndex([])
        return ts, np.array([d["Average"] for d in pts], dtype=float)

    def logs(self, iid, start, end, limit=3000):
        """
        Lines from the CloudWatch Logs group, one stream per instance id. Returns
        nothing when no group is configured, so log clustering is simply skipped.
        Checked against moto only.
        """
        if not self.log_group:
            return []
        out, token = [], None
        while len(out) < limit:
            kw = dict(logGroupName=self.log_group, logStreamNames=[iid], limit=1000,
                      startTime=int(pd.Timestamp(start).timestamp() * 1000), endTime=int(pd.Timestamp(end).timestamp() * 1000))
            if token:
                kw["nextToken"] = token
            r = self.logs_client.filter_log_events(**kw)
            out += [(pd.Timestamp(e["timestamp"], unit="ms"), e["message"]) for e in r.get("events", [])]
            token = r.get("nextToken")
            if not token:
                break
        return out
