"""Plain data shapes passed between the engine's stages."""
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Instance:
    id: str
    role: str
    itype: str


@dataclass
class Signal:
    ts: datetime
    instance_id: str
    metric: str
    kind: str            # anomaly | limit | forecast
    severity: str        # warning | critical
    value: float
    detail: str = ""
    direction: str = ""  # up | down, for anomalies
    eta_min: float = None


@dataclass
class Diagnosis:
    cause: str           # disk_fill, memory_leak, runaway_process, traffic_surge, ...
    scope: str           # instance | tier
    target: str          # instance id or role
    severity: str
    evidence: list = field(default_factory=list)


@dataclass
class Decision:
    ts: datetime
    incident_id: int
    cause: str
    action: str          # disk_cleanup, restart_service, reboot, scale_out, migrate_host, enable_unlimited, notify, none
    target: str
    status: str          # executed | shadow | needs_approval | blocked | notify | none
    reasons: list = field(default_factory=list)


@dataclass
class Incident:
    id: int
    cause: str
    scope: str
    target: str
    opened: datetime
    severity: str
    status: str = "open"       # open | mitigated | resolved | escalated
    closed: datetime = None
    attempts: dict = field(default_factory=dict)   # action -> how many times tried
    evidence: list = field(default_factory=list)
    last_seen: datetime = None


def as_dict(obj):
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
