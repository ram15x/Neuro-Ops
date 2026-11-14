import numpy as np
import pandas as pd
import pytest

from backend.detect.detectors import DETECTORS
from backend.detect.forecast import time_to_breach


def _series(n=1500, seed=0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-03-02", periods=n, freq="5min")
    hour = np.asarray(ts.hour + ts.minute / 60, dtype=float)
    values = 30 + 10 * np.sin(hour / 24 * 2 * np.pi) + rng.normal(0, 1, n)
    return ts, values


@pytest.mark.parametrize("name", list(DETECTORS))
def test_detectors_never_look_ahead(name):
    # changing the future must not change any score from the past
    ts, v = _series()
    cut, warm = 1000, 300
    changed = v.copy()
    changed[cut:] = 95
    a = DETECTORS[name](v, ts, warm)
    b = DETECTORS[name](changed, ts, warm)
    assert np.allclose(a[:cut], b[:cut])


@pytest.mark.parametrize("name", [n for n in DETECTORS if n != "static_threshold"])
def test_a_big_spike_scores_above_everything_normal(name):
    ts, v = _series()
    v[1200:1206] = 97
    score = DETECTORS[name](v, ts, 300)
    assert score[1200:1206].max() > np.quantile(score[300:1200], 0.999)


def test_forecast_on_a_steady_ramp():
    rng = np.random.default_rng(1)
    v = 40 + 0.1 * np.arange(300) + rng.normal(0, 0.3, 300)
    b = time_to_breach(v, limit=90)
    truth = (90 - v[-1]) / 0.1
    assert b is not None
    assert abs(b.eta_steps - truth) < 0.2 * truth
    assert b.eta_low <= b.eta_steps <= b.eta_high


def test_forecast_stays_quiet_on_flat_data():
    rng = np.random.default_rng(2)
    assert time_to_breach(50 + rng.normal(0, 0.5, 300), limit=90) is None


def test_forecast_ignores_a_one_off_jump():
    # a deploy that uses more memory and then stays flat is not a leak
    rng = np.random.default_rng(3)
    v = np.r_[np.full(200, 40.0), np.full(100, 55.0)] + rng.normal(0, 0.4, 300)
    assert time_to_breach(v, limit=90) is None


def test_forecast_works_downward_for_credits():
    v = 400 - 2.0 * np.arange(120)
    b = time_to_breach(v, limit=5, rising=False)
    assert b is not None
    assert abs(b.eta_steps - (v[-1] - 5) / 2.0) < 3


def test_forecast_needs_enough_history():
    assert time_to_breach([50, 51, 52], limit=90) is None
