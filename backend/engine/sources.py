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

    def __init__(self, region, lookback_days=14, session=None):
        import boto3
        s = session or boto3.session.Session(region_name=region)
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
