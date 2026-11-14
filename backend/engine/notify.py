"""
Tells a person. Written for whoever is on call at 3am: what broke, where, how
long, what the engine already tried, what the logs say, and what to check first.

Channels: SNS (email, SMS, whatever is subscribed), a Slack webhook, and a
recorder that only remembers what it would have sent. The recorder is what the
simulated demo uses, so the demo never emails anybody.
"""
import json
import urllib.request
from datetime import timedelta

# first things to look at per cause. plain advice, not magic.
CHECK_FIRST = {
    "disk_fill": "Find what is writing: `du -xh / | sort -h | tail`. Logs and core dumps are the usual cause.",
    "memory_leak": "Look at which process grows: `ps aux --sort=-rss | head`. Check the last release.",
    "runaway_process": "Find the busy process with `top`. A loop after a deploy is the usual cause.",
    "traffic_surge": "Check the load balancer request count and whether the spike is real users or one client.",
    "cpu_contention": "One host is slow while peers are fine. Check CPU steal (`top`, the %st column) and consider stop/start to move host.",
    "credit_exhaustion": "Burst credits ran out. Either find what keeps the CPU busy or move to a larger or unlimited instance.",
    "network_loss": "Traffic stopped. Check security groups, the load balancer target health and the upstream.",
    "memory_step": "Memory jumped and stayed. Usually a release. Check the deploy log.",
}

TITLES = {
    "disk_fill": "Disk filling up", "memory_leak": "Memory leak", "runaway_process": "Runaway process",
    "traffic_surge": "Traffic surge", "cpu_contention": "CPU contention", "credit_exhaustion": "Burst credits running out",
    "network_loss": "Traffic dropped", "memory_step": "Memory jumped",
}

ACTION_WORDS = {"disk_cleanup": "clean up disk", "restart_service": "restart the service", "reboot": "reboot",
                "scale_out": "add an instance", "migrate_host": "move to another host",
                "enable_unlimited": "switch to unlimited credits"}


def _minutes(a, b):
    return max(0, int((b - a) / timedelta(minutes=1)))


def build_alert(inc, why, trail, now, simulated, dashboard_url=None, resolved=False):
    """
    inc: the Incident, why: reasons the engine gives, trail: audit events for this incident,
    now: time of the message. Returns dict(subject, text, severity, incident).
    """
    title = TITLES.get(inc.cause, inc.cause)
    where = f"the {inc.target} tier" if inc.scope == "tier" else inc.target
    tag = "[SIMULATED] " if simulated else ""
    since = _minutes(inc.opened, now)
    if resolved:
        subject = f"{tag}Resolved: {title} on {where} after {since} min"
    else:
        subject = f"{tag}{inc.severity.upper()}: {title} on {where}, needs a person"
    lines = [subject, ""]
    if simulated:
        lines += ["This came from the simulated fleet. Nothing real is wrong.", ""]
    lines += [f"What: {title} on {where}", f"Open for: {since} minutes (since {inc.opened:%Y-%m-%d %H:%M})"]
    if why and not resolved:
        lines += ["Why you are being told: " + "; ".join(why)]
    lines += ["", "Evidence:"] + [f"  - {e}" for e in inc.evidence[:4]]

    tried = [t for t in trail if t["type"] in ("action", "verified", "verify_failed", "approved", "rejected", "rolled_back")]
    if tried:
        lines += ["", "What the engine already did:"]
        for t in tried:
            a = ACTION_WORDS.get(t.get("action"), t.get("action", ""))
            word = {"action": "ran", "verified": "worked", "verify_failed": "did not help", "approved": "approved by " + str(t.get("by")),
                    "rejected": "rejected by " + str(t.get("by")), "rolled_back": "undone"}[t["type"]]
            lines.append(f"  - {t['ts'][11:16]} {a}: {word}")
    elif not resolved:
        lines += ["", "The engine has not changed anything on this target."]

    if inc.change:
        lines += ["", f"Possible trigger: something was deployed {inc.change['minutes_before']} minutes before this started "
                      f"(\"{inc.change['text']}\"). Check that release first."]
    if inc.logs:
        lines += ["", "Log lines that are new or louder than usual:"]
        for c in inc.logs[:3]:
            new = "new" if c["new"] else f"{c['in_window']} now vs {c['in_baseline']} in the last day"
            lines.append(f"  - [{c['level']}] {c['template'][:110]} ({new})")
    if not resolved and inc.cause in CHECK_FIRST:
        lines += ["", "Check first: " + CHECK_FIRST[inc.cause]]
    if dashboard_url:
        lines += ["", f"Dashboard: {dashboard_url}"]
    return dict(subject=subject[:100], text="\n".join(lines), severity=inc.severity, incident=inc.id)


class RecordingNotifier:
    """remembers what it would have sent. nothing leaves the process."""
    channel = "demo (not sent)"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return dict(ok=True, channel=self.channel)


class SnsNotifier:
    channel = "sns"

    def __init__(self, topic_arn, region, session=None):
        import boto3
        self.arn = topic_arn
        self.client = (session or boto3.session.Session(region_name=region)).client("sns", region_name=region)

    def send(self, alert):
        self.client.publish(TopicArn=self.arn, Subject=alert["subject"], Message=alert["text"])
        return dict(ok=True, channel=self.channel)


class SlackNotifier:
    channel = "slack"

    def __init__(self, webhook_url, post=None):
        self.url = webhook_url
        self.post = post or self._post

    @staticmethod
    def _post(url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def send(self, alert):
        status = self.post(self.url, {"text": f"*{alert['subject']}*\n```{alert['text']}```"})
        return dict(ok=status == 200, channel=self.channel)


class Notifier:
    """
    Wraps one or more channels with the rules that keep it from being noise:
    one message per incident per kind, and a cap per hour (the rest are counted
    and mentioned in the next message that does go out).
    """

    def __init__(self, channels, max_per_hour=10):
        self.channels = channels
        self.cap = max_per_hour
        self.sent_times = []
        self.done = set()
        self.suppressed = 0

    def notify(self, alert, kind, now):
        key = (alert["incident"], kind)
        if key in self.done:
            return []
        self.done.add(key)
        self.sent_times = [t for t in self.sent_times if now - t < timedelta(hours=1)]
        if len(self.sent_times) >= self.cap:
            self.suppressed += 1
            return [dict(ok=False, channel="all", error=f"over {self.cap} alerts in the last hour, held back")]
        if self.suppressed:
            alert = {**alert, "text": alert["text"] + f"\n\n({self.suppressed} other alert(s) were held back by the hourly cap.)"}
            self.suppressed = 0
        self.sent_times.append(now)
        results = []
        for ch in self.channels:
            try:
                results.append(ch.send(alert))
            except Exception as e:   # a dead webhook must not stop the others, or the engine
                results.append(dict(ok=False, channel=getattr(ch, "channel", "?"), error=f"{type(e).__name__}: {e}"))
        return results


def from_settings(cfg, simulated, region=None):
    """
    Real channels only when they are configured, and never for the simulated
    fleet unless ALERT_SEND_SIMULATED is on. Otherwise a recorder.
    """
    channels = []
    if (not simulated) or cfg.ALERT_SEND_SIMULATED:
        if cfg.ALERT_SNS_TOPIC_ARN:
            channels.append(SnsNotifier(cfg.ALERT_SNS_TOPIC_ARN, region or cfg.AWS_REGION))
        if cfg.ALERT_SLACK_WEBHOOK:
            channels.append(SlackNotifier(cfg.ALERT_SLACK_WEBHOOK))
    return Notifier(channels or [RecordingNotifier()], cfg.ALERT_MAX_PER_HOUR)
