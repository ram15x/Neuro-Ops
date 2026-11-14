"""
Turns raw metrics into signals: something is unusual (anomaly), something is
over a hard line (limit), or something will be over the line soon (forecast).
"""
import json
import os

import numpy as np

from backend.data.loaders import KIND
from backend.detect.forecast import time_to_breach
from backend.engine.model import Signal

_HERE = os.path.dirname(__file__)
THRESHOLDS = json.load(open(os.path.join(_HERE, "..", "detect", "thresholds.json")))

# hard lines: (warning, critical). credits count down, the rest count up
LIMITS = {
    "CPUUtilization": (None, 95.0),
    "mem_used_percent": (85.0, 90.0),
    "disk_used_percent": (80.0, 90.0),
    "CPUCreditBalance": (60.0, 20.0),
}
# what the forecast watches: (limit, rising)
FORECASTS = {
    "mem_used_percent": (90.0, True),
    "disk_used_percent": (90.0, True),
    "CPUCreditBalance": (5.0, False),
}
HORIZON_STEPS = 72   # only warn about breaches inside the next 6 hours


def limit_signal(ts, iid, metric, value):
    if metric not in LIMITS:
        return None
    warn, crit = LIMITS[metric]
    falling = metric == "CPUCreditBalance"
    over = (lambda line: value <= line) if falling else (lambda line: value >= line)
    if over(crit):
        return Signal(ts, iid, metric, "limit", "critical", float(value), f"{metric} at {value:.1f}, critical line {crit}")
    if warn is not None and over(warn):
        return Signal(ts, iid, metric, "limit", "warning", float(value), f"{metric} at {value:.1f}, warning line {warn}")
    return None


def anomaly_signal(ts, iid, metric, values, score):
    kind = KIND.get(metric)
    thr = THRESHOLDS["thresholds"].get(kind)
    if thr is None or not np.isfinite(score) or score <= thr:
        return None
    recent = np.median(values[-289:-1]) if len(values) > 24 else values[0]
    direction = "up" if values[-1] >= recent else "down"
    sev = "critical" if score > 2 * thr else "warning"
    return Signal(ts, iid, metric, "anomaly", sev, float(values[-1]),
                  f"{metric} unusual ({direction}), score {score:.1f} over threshold {thr:.1f}", direction)


def forecast_signal(ts, iid, metric, values):
    if metric not in FORECASTS or len(values) < 40:
        return None
    limit, rising = FORECASTS[metric]
    # cheap pre-check so the line fit only runs when the metric is actually moving
    moved = values[-1] - values[-25]
    if (rising and moved < 1.5) or (not rising and moved > -10):
        return None
    b = time_to_breach(values, limit, rising, horizon=HORIZON_STEPS)
    if b is None:
        return None
    eta = b.eta_steps * 5
    sev = "critical" if eta <= 60 else "warning"
    return Signal(ts, iid, metric, "forecast", sev, float(values[-1]),
                  f"{metric} heading for {limit} in about {eta:.0f} min "
                  f"(range {b.eta_low * 5:.0f} to {b.eta_high * 5:.0f})", "up" if rising else "down", eta)
