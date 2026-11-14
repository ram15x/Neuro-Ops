from datetime import datetime, timedelta


from backend.engine.model import Incident
from backend.engine.notify import Notifier, RecordingNotifier, SlackNotifier, SnsNotifier, build_alert

T = datetime(2026, 3, 10, 12, 0)


def incident(**kw):
    inc = Incident(7, "disk_fill", "instance", "i-0a1web0000000001", T - timedelta(minutes=40), "critical",
                   evidence=["disk_used_percent at 93%, full in about 25 min"])
    inc.logs = [dict(template="ERROR write failed: No space left on device path=<*>", level="error", in_window=9,
                     in_baseline=0, new=True, score=18.0, example="")]
    for k, v in kw.items():
        setattr(inc, k, v)
    return inc


def test_alert_says_what_where_how_long_and_what_to_check():
    trail = [dict(type="action", ts="2026-03-10T11:40:00", action="disk_cleanup"),
             dict(type="verify_failed", ts="2026-03-10T11:55:00", action="disk_cleanup")]
    a = build_alert(incident(), ["no safe automatic fix left"], trail, T, simulated=False, dashboard_url="https://x.test")
    assert a["subject"].startswith("CRITICAL: Disk filling up on i-0a1web0000000001")
    t = a["text"]
    for want in ("40 minutes", "clean up disk: ran", "clean up disk: did not help", "No space left on device", "Check first", "https://x.test"):
        assert want in t


def test_simulated_alerts_are_marked():
    a = build_alert(incident(), [], [], T, simulated=True)
    assert a["subject"].startswith("[SIMULATED]") and "Nothing real is wrong" in a["text"]


def test_one_message_per_incident_and_kind():
    n = Notifier([RecordingNotifier()])
    a = build_alert(incident(), [], [], T, False)
    assert len(n.notify(a, "escalated", T)) == 1
    assert n.notify(a, "escalated", T) == []
    assert len(n.notify(a, "resolved", T)) == 1


def test_hourly_cap_holds_messages_back_and_mentions_it_later():
    rec = RecordingNotifier()
    n = Notifier([rec], max_per_hour=2)
    for i in range(1, 4):
        n.notify({**build_alert(incident(), [], [], T, False), "incident": i}, "escalated", T)
    assert len(rec.sent) == 2 and n.suppressed == 1
    n.notify({**build_alert(incident(), [], [], T, False), "incident": 9}, "escalated", T + timedelta(hours=2))
    assert "held back" in rec.sent[-1]["text"]


def test_a_dead_channel_does_not_stop_the_others():
    class Dead:
        channel = "dead"

        def send(self, a):
            raise ConnectionError("no route")
    rec = RecordingNotifier()
    out = Notifier([Dead(), rec]).notify(build_alert(incident(), [], [], T, False), "escalated", T)
    assert [r["ok"] for r in out] == [False, True] and "ConnectionError" in out[0]["error"]


def test_slack_posts_the_text():
    seen = {}
    SlackNotifier("https://hooks.test/x", post=lambda u, b: seen.update(url=u, body=b) or 200).send(
        build_alert(incident(), [], [], T, False))
    assert seen["url"] == "https://hooks.test/x" and "Disk filling up" in seen["body"]["text"]


def test_sns_delivers_to_a_subscribed_queue():
    import json
    import boto3
    from moto import mock_aws
    with mock_aws():
        sns, sqs = boto3.client("sns", region_name="us-east-1"), boto3.client("sqs", region_name="us-east-1")
        arn = sns.create_topic(Name="alerts")["TopicArn"]
        q = sqs.create_queue(QueueName="q")["QueueUrl"]
        sns.subscribe(TopicArn=arn, Protocol="sqs", Endpoint=sqs.get_queue_attributes(
            QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"])
        SnsNotifier(arn, "us-east-1").send(build_alert(incident(), [], [], T, False))
        body = json.loads(sqs.receive_message(QueueUrl=q)["Messages"][0]["Body"])
        assert "Disk filling up" in body["Subject"] and "No space left" in body["Message"]
