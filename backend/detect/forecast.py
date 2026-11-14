"""
Time-to-breach forecast.

Fits a straight line to the recent past and asks when it will hit a limit
(disk at 90%, burst credits at zero). A straight line is wrong for leaks that
speed up, and a one-off jump can look like a trend, so a warning needs the
trend to hold over several checks in a row and comes with a range.
"""
from dataclasses import dataclass

import numpy as np
from scipy.stats import theilslopes


@dataclass
class Breach:
    eta_steps: float       # best guess, in datapoints from now
    eta_low: float         # earliest plausible
    eta_high: float        # latest plausible (inf when the slow end never gets there)


def _fit(window, limit, rising):
    x = np.arange(len(window))
    # theil-sen takes the median slope, so a few spikes don't drag the line
    slope, intercept, lo, hi = theilslopes(window, x, 0.90)
    now = intercept + slope * (len(window) - 1)
    gap = (limit - now) if rising else (now - limit)
    if gap <= 0:
        return 0.0, 0.0, 0.0
    if not rising:
        slope, lo, hi = -slope, -hi, -lo
    if slope <= 0 or lo <= 0:
        # the slow end of the range is flat or going the other way: no clear trend
        return None
    return gap / slope, gap / hi, gap / lo


def time_to_breach(values, limit=90.0, rising=True, window=24, confirm=3, gap=6, horizon=None):
    """
    values: the series up to now. Returns a Breach, or None when there is no
    steady trend toward the limit.

    window:  points per fit (24 = two hours of 5-minute data)
    confirm: how many fits in a row must agree, each `gap` points apart
    horizon: ignore breaches further out than this many points
    """
    values = np.asarray(values, dtype=float)
    need = window + (confirm - 1) * gap
    if len(values) < need:
        return None
    fits = []
    for k in range(confirm):
        end = len(values) - k * gap
        f = _fit(values[end - window:end], limit, rising)
        if f is None:
            return None
        fits.append(f)
    eta, low, high = fits[0]
    if horizon is not None and eta > horizon:
        return None
    return Breach(eta, low, high)
