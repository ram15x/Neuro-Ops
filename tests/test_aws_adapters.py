"""
The AWS code is checked against moto, a local fake of AWS. This proves the
calls are well formed. It does not prove IAM, timing or real behaviour.
"""
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from backend.engine.executors import AwsExecutor
from backend.engine.sources import CloudWatchSource

REGION = "ap-south-1"


@pytest.fixture
def aws(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        iid = ec2.run_instances(ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1, TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": "Role", "Value": "web"}]}])["Instances"][0]["InstanceId"]
        yield dict(ec2=ec2, ami=ami, iid=iid)


def _state(ec2, iid):
    return ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]


def test_source_lists_running_instances_with_their_role(aws):
    found = CloudWatchSource(REGION).instances()
    assert [(i.id, i.role, i.itype) for i in found] == [(aws["iid"], "web", "t3.small")]


def test_source_reads_agent_metrics_in_time_order(aws):
    cw = boto3.client("cloudwatch", region_name=REGION)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for minutes, value in ((15, 40.0), (10, 45.0), (5, 50.0)):
        cw.put_metric_data(Namespace="CWAgent", MetricData=[{
            "MetricName": "mem_used_percent", "Value": value, "Timestamp": now - timedelta(minutes=minutes),
            "Dimensions": [{"Name": "InstanceId", "Value": aws["iid"]}]}])
    ts, values = CloudWatchSource(REGION, lookback_days=1).history_at(aws["iid"], "mem_used_percent", now)
    assert len(values) >= 1
    assert list(ts) == sorted(ts)
    assert 40.0 <= values.min() and values.max() <= 50.0


def test_shell_actions_go_through_systems_manager(aws):
    ex = AwsExecutor(REGION)
    for action in ("disk_cleanup", "restart_service"):
        res = ex.run(action, aws["iid"], {})
        assert res["ok"], res
    sent = boto3.client("ssm", region_name=REGION).list_commands()["Commands"]
    assert len(sent) == 2
    assert all(c["DocumentName"] == "AWS-RunShellScript" and c["InstanceIds"] == [aws["iid"]] for c in sent)


def test_reboot_and_migrate_leave_the_instance_running(aws):
    ex = AwsExecutor(REGION)
    assert ex.run("reboot", aws["iid"], {})["ok"]
    assert ex.run("migrate_host", aws["iid"], {})["ok"]
    assert _state(aws["ec2"], aws["iid"])["State"]["Name"] == "running"


def test_unlimited_credits_and_undo(aws):
    # moto has not implemented this call, so the request is checked against
    # botocore's own model of the API instead (wrong parameters would raise)
    from botocore.stub import Stubber
    ex = AwsExecutor(REGION)
    ok = {"SuccessfulInstanceCreditSpecifications": [{"InstanceId": aws["iid"]}]}
    with Stubber(ex.ec2) as stub:
        stub.add_response("modify_instance_credit_specification", ok, {
            "InstanceCreditSpecifications": [{"InstanceId": aws["iid"], "CpuCredits": "unlimited"}]})
        stub.add_response("modify_instance_credit_specification", ok, {
            "InstanceCreditSpecifications": [{"InstanceId": aws["iid"], "CpuCredits": "standard"}]})
        res = ex.run("enable_unlimited", aws["iid"], {})
        assert res["ok"] and res["undo"] == {"mode": "standard"}
        assert ex.undo("enable_unlimited", aws["iid"], res["undo"], {})["ok"]
        stub.assert_no_pending_responses()


def test_scale_out_respects_the_group_max_and_can_be_undone(aws):
    asg = boto3.client("autoscaling", region_name=REGION)
    asg.create_launch_configuration(LaunchConfigurationName="lc", ImageId=aws["ami"], InstanceType="t3.small")
    asg.create_auto_scaling_group(AutoScalingGroupName="web-asg", LaunchConfigurationName="lc", MinSize=1, MaxSize=3,
                                  DesiredCapacity=2, AvailabilityZones=[REGION + "a"],
                                  Tags=[{"Key": "Role", "Value": "web", "PropagateAtLaunch": True}])
    ex = AwsExecutor(REGION)
    want = lambda: asg.describe_auto_scaling_groups(AutoScalingGroupNames=["web-asg"])["AutoScalingGroups"][0]["DesiredCapacity"]
    first = ex.run("scale_out", "web", {})
    assert first["ok"] and want() == 3
    # already at max: must refuse, not push past it
    assert not ex.run("scale_out", "web", {})["ok"] and want() == 3
    assert ex.undo("scale_out", "web", first["undo"], {})["ok"] and want() == 2


def test_scale_out_without_a_group_fails_cleanly(aws):
    res = AwsExecutor(REGION).run("scale_out", "api", {})
    assert not res["ok"] and "no auto scaling group" in res["detail"]


def test_aws_errors_come_back_as_a_result_not_a_crash(aws):
    res = AwsExecutor(REGION).run("reboot", "i-0000000000000dead", {})
    assert not res["ok"] and res["detail"]


def test_unknown_action_and_undo_are_refused(aws):
    ex = AwsExecutor(REGION)
    assert not ex.run("format_disk", aws["iid"], {})["ok"]
    assert not ex.undo("reboot", aws["iid"], None, {})["ok"]
