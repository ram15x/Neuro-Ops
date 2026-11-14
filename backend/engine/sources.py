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
    Real AWS, read only. Checked against moto (a fake of AWS, also run as a real
    HTTP server in tests), never against a live account: that proves the request
    shapes and the paging, not IAM, throttling, or what real instances do.
    """
    simulated = False
    replayable = False

    NAMESPACE = {"CPUUtilization": "AWS/EC2", "NetworkIn": "AWS/EC2", "CPUCreditBalance": "AWS/EC2",
                 "mem_used_percent": "CWAgent", "disk_used_percent": "CWAgent"}
    # NetworkIn is bytes per period, so it is a sum. the rest are levels.
    STAT = {"NetworkIn": "Sum"}
    MANAGED_TAG = "neuroops:managed"

    def __init__(self, region, lookback_days=14, session=None, log_group=None):
        import boto3
        s = session or boto3.session.Session(region_name=region)
        self.ec2 = s.client("ec2", region_name=region)
        self.cw = s.client("cloudwatch", region_name=region)
        self.logs_client = s.client("logs", region_name=region)
        self.log_group = log_group   # one group, one stream per instance id (the CloudWatch agent default)
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
                    out.append(Instance(i["InstanceId"], tags.get("Role", i["InstanceId"]), i["InstanceType"],
                                        managed=tags.get(self.MANAGED_TAG) == "true"))
        return out

    def metrics(self, iid):
        return list(METRICS)

    def history_many(self, iid, now):
        """
        Every metric for one instance in one GetMetricData call (paged), on a clean
        5 minute grid. Returns {metric: (timestamps, values)} plus "_last_seen",
        the time of the newest data point of any metric, or None if there is none.
        """
        queries = [{"Id": f"m{k}", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": self.NAMESPACE[m], "MetricName": m, "Dimensions": [{"Name": "InstanceId", "Value": iid}]},
            "Period": 300, "Stat": self.STAT.get(m, "Average")}} for k, m in enumerate(METRICS)]
        got = {q["Id"]: ([], []) for q in queries}
        token = None
        while True:
            kw = dict(MetricDataQueries=queries, StartTime=now - self.lookback, EndTime=now, ScanBy="TimestampAscending")
            if token:
                kw["NextToken"] = token
            r = self.cw.get_metric_data(**kw)
            for res in r["MetricDataResults"]:
                got[res["Id"]][0].extend(res["Timestamps"])
                got[res["Id"]][1].extend(res["Values"])
            token = r.get("NextToken")
            if not token:
                break
        out, last = {}, None
        for k, m in enumerate(METRICS):
            stamps, vals = got[f"m{k}"]
            if not stamps:
                continue
            s = pd.Series(vals, index=pd.DatetimeIndex(stamps).tz_localize(None) if pd.DatetimeIndex(stamps).tz is None
                          else pd.DatetimeIndex(stamps).tz_convert(None)).sort_index()
            s.index = s.index.floor("5min")   # real data is already on the period, this just tolerates a stray second
            s = s[~s.index.duplicated(keep="last")]
            grid = pd.date_range(s.index[0].floor("5min"), s.index[-1].floor("5min"), freq="5min")
            s = s.reindex(grid).ffill(limit=3).interpolate(limit=6).ffill().bfill()   # short holes are filled, a long hole repeats the last value
            out[m] = (grid, s.to_numpy(dtype=float))
            last = grid[-1] if last is None or grid[-1] > last else last
        out["_last_seen"] = last.to_pydatetime() if last is not None else None
        return out

    def history_at(self, iid, metric, now):
        ts, values = self.history_many(iid, now).get(metric, (pd.DatetimeIndex([]), np.array([])))
        return ts, values

    def logs(self, iid, start, end, limit=3000):
        """
        Lines from the CloudWatch Logs group, one stream per instance id. Returns
        nothing when no group is configured, so log clustering is simply skipped.
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
