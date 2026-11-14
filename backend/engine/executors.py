"""
The hands. One interface, two implementations: the simulated fleet and real
AWS through boto3. The engine does not know which one it is holding.
"""
from abc import ABC, abstractmethod


class Executor(ABC):
    @abstractmethod
    def run(self, action, target, context):
        """returns dict(ok, detail, undo) where undo is None or arguments for undo()"""

    @abstractmethod
    def undo(self, action, target, undo, context):
        """reverses an earlier action, returns dict(ok, detail)"""


class SimExecutor(Executor):
    """Acts on the simulated fleet, so later metrics really change."""

    def __init__(self, fleet):
        self.fleet = fleet

    def run(self, action, target, context):
        step = context["step"] + 1   # an action lands on the next datapoint, not instantly
        f = self.fleet
        if step >= f.n:
            return dict(ok=False, detail="end of simulation", undo=None)
        if action == "scale_out":
            f.scale(target, step, +1)
            return dict(ok=True, detail=f"SIMULATED: added one {target} instance", undo={"delta": -1})
        if action == "enable_unlimited":
            f.set_unlimited(target, step, True)
            return dict(ok=True, detail="SIMULATED: switched to unlimited credits", undo={"on": False})
        fn = {"disk_cleanup": f.disk_cleanup, "restart_service": f.restart_service,
              "reboot": f.reboot, "migrate_host": f.migrate_host}.get(action)
        if fn is None:
            return dict(ok=False, detail=f"unknown action {action}", undo=None)
        fn(target, step)
        return dict(ok=True, detail=f"SIMULATED: {action} on {target}", undo=None)

    def undo(self, action, target, undo, context):
        step = context["step"] + 1
        if action == "scale_out":
            self.fleet.scale(target, step, undo["delta"])
            return dict(ok=True, detail=f"SIMULATED: removed the extra {target} instance")
        if action == "enable_unlimited":
            self.fleet.set_unlimited(target, step, False)
            return dict(ok=True, detail="SIMULATED: back to standard credits")
        return dict(ok=False, detail=f"{action} cannot be undone")


class AwsExecutor(Executor):
    """
    Real AWS calls. Checked against moto only, never against a live account.
    Commands run on the box go through Systems Manager, so no SSH keys.
    """

    CLEANUP = ["journalctl --vacuum-time=2d || true",
               "find /var/log -name '*.gz' -mtime +7 -delete || true",
               "find /tmp -type f -mtime +3 -delete || true", "df -h /"]

    TAG = "neuroops:managed"

    def __init__(self, region, service_name="neuroops", session=None, require_tag=True):
        import boto3
        s = session or boto3.session.Session(region_name=region)
        self.ec2 = s.client("ec2", region_name=region)
        self.ssm = s.client("ssm", region_name=region)
        self.asg = s.client("autoscaling", region_name=region)
        self.service_name = service_name
        self.require_tag = require_tag

    def _shell(self, iid, commands, comment):
        r = self.ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                                  Parameters={"commands": commands}, Comment=comment)
        return r["Command"]["CommandId"]

    def _group_for_role(self, role):
        for page in self.asg.get_paginator("describe_auto_scaling_groups").paginate():
            for g in page["AutoScalingGroups"]:
                tags = {t["Key"]: t["Value"] for t in g.get("Tags", [])}
                if tags.get("Role") == role or g["AutoScalingGroupName"] == role:
                    return g
        return None

    def _refusal(self, action, target):
        """code-level twin of the IAM rule: act only on things tagged neuroops:managed=true"""
        if not self.require_tag:
            return None
        if action == "scale_out":
            g = self._group_for_role(target)
            if g is None:
                return f"no auto scaling group for {target}"
            tags = {t["Key"]: t["Value"] for t in g.get("Tags", [])}
            return None if tags.get(self.TAG) == "true" else f"auto scaling group for {target} is not tagged {self.TAG}=true"
        res = self.ec2.describe_instances(InstanceIds=[target])["Reservations"]
        tags = {t["Key"]: t["Value"] for r in res for i in r["Instances"] for t in i.get("Tags", [])}
        return None if tags.get(self.TAG) == "true" else f"{target} is not tagged {self.TAG}=true"

    def run(self, action, target, context):
        try:
            why = self._refusal(action, target)
            if why:
                return dict(ok=False, detail=f"refused: {why}", undo=None)
            if action == "disk_cleanup":
                cid = self._shell(target, self.CLEANUP, "neuroops disk cleanup")
                return dict(ok=True, detail=f"cleanup sent, command {cid}", undo=None)
            if action == "restart_service":
                cid = self._shell(target, [f"systemctl restart {self.service_name}"], "neuroops restart service")
                return dict(ok=True, detail=f"restart sent, command {cid}", undo=None)
            if action == "reboot":
                self.ec2.reboot_instances(InstanceIds=[target])
                return dict(ok=True, detail="reboot requested", undo=None)
            if action == "migrate_host":
                # stop then start puts the instance on different hardware
                self.ec2.stop_instances(InstanceIds=[target])
                self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[target])
                self.ec2.start_instances(InstanceIds=[target])
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[target])
                return dict(ok=True, detail="stopped and started on a new host", undo=None)
            if action == "enable_unlimited":
                self.ec2.modify_instance_credit_specification(
                    InstanceCreditSpecifications=[{"InstanceId": target, "CpuCredits": "unlimited"}])
                return dict(ok=True, detail="credit mode set to unlimited", undo={"mode": "standard"})
            if action == "scale_out":
                g = self._group_for_role(target)
                if g is None:
                    return dict(ok=False, detail=f"no auto scaling group for {target}", undo=None)
                want = g["DesiredCapacity"] + 1
                if want > g["MaxSize"]:
                    return dict(ok=False, detail=f"group is at its max size {g['MaxSize']}", undo=None)
                self.asg.set_desired_capacity(AutoScalingGroupName=g["AutoScalingGroupName"],
                                              DesiredCapacity=want, HonorCooldown=False)
                return dict(ok=True, detail=f"desired capacity {g['DesiredCapacity']} -> {want}",
                            undo={"group": g["AutoScalingGroupName"], "capacity": g["DesiredCapacity"]})
            return dict(ok=False, detail=f"unknown action {action}", undo=None)
        except Exception as e:
            return dict(ok=False, detail=f"{type(e).__name__}: {e}", undo=None)

    def undo(self, action, target, undo, context):
        try:
            if action == "enable_unlimited":
                why = self._refusal(action, target)
                if why:
                    return dict(ok=False, detail=f"refused: {why}")
            if action == "scale_out":
                self.asg.set_desired_capacity(AutoScalingGroupName=undo["group"],
                                              DesiredCapacity=undo["capacity"], HonorCooldown=False)
                return dict(ok=True, detail=f"desired capacity back to {undo['capacity']}")
            if action == "enable_unlimited":
                self.ec2.modify_instance_credit_specification(
                    InstanceCreditSpecifications=[{"InstanceId": target, "CpuCredits": undo["mode"]}])
                return dict(ok=True, detail="credit mode back to standard")
            return dict(ok=False, detail=f"{action} cannot be undone")
        except Exception as e:
            return dict(ok=False, detail=f"{type(e).__name__}: {e}")
