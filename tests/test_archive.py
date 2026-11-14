import boto3
from moto import mock_aws

from backend.storage.archive import S3Archive


def events(day, n):
    return [{"ts": f"{day}T10:{i:02d}:00", "type": "decision", "incident": i} for i in range(n)]


@mock_aws
def test_events_are_stored_per_day_and_read_back():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="neuroops-test")
    a = S3Archive("neuroops-test", "us-east-1")
    keys = a.put(7, events("2026-03-10", 3) + events("2026-03-11", 2))
    assert len(keys) == 2 and "run=7/date=2026-03-10" in keys[0]
    assert [e["incident"] for e in a.read(7, "2026-03-10")] == [0, 1, 2]
    assert len(a.read(7, "2026-03-11")) == 2


@mock_aws
def test_a_second_write_for_the_same_day_does_not_overwrite():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="neuroops-test")
    a = S3Archive("neuroops-test", "us-east-1")
    a.put(7, events("2026-03-10", 2))
    a.put(7, events("2026-03-10", 3))
    assert len(a.read(7, "2026-03-10")) == 5


@mock_aws
def test_objects_are_encrypted():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="neuroops-test")
    key = S3Archive("neuroops-test", "us-east-1").put(1, events("2026-03-10", 1))[0]
    assert s3.head_object(Bucket="neuroops-test", Key=key)["ServerSideEncryption"] == "AES256"
