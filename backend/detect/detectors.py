"""
Anomaly detectors.

Every detector takes a series and returns one score per point, higher meaning
more unusual. They only ever look backwards, so the score at time t never uses
data from after t. The first `n_warm` points are a learning period.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

DAY = 288  # 5-minute points in a day


def prepare(values, kind):
    """
    Traffic is multiplicative: it doubles or drops to a tenth, it does not move
    by a fixed number of bytes. On a log scale a drop to 3% is as loud as a
    30x spike, which is what we want. Everything else is used as it is.
    """
    values = np.asarray(values, dtype=float)
    if kind == "network":
        return np.log1p(np.clip(values, 0, None))
    return values


def static_threshold(values, timestamps=None, n_warm=0):
    # the classic "alert when cpu > X". the score is just the value
    return np.asarray(values, dtype=float)


def rolling_zscore(values, timestamps=None, n_warm=0, window=DAY):
    # how many standard deviations away from the last day's average
    s = pd.Series(np.asarray(values, dtype=float))
    past = s.shift(1).rolling(window, min_periods=24)
    std = past.std().replace(0, np.nan)
    return ((s - past.mean()).abs() / std).fillna(0).to_numpy()


def ewma(values, timestamps=None, n_warm=0, alpha=0.05):
    # same idea as z-score but recent points count more
    s = pd.Series(np.asarray(values, dtype=float))
    mean = s.ewm(alpha=alpha, adjust=False).mean().shift(1)
    var = ((s - mean) ** 2).ewm(alpha=alpha, adjust=False).mean().shift(1)
    return ((s - mean).abs() / np.sqrt(var.replace(0, np.nan))).fillna(0).to_numpy()


def seasonal_band(values, timestamps, n_warm=0):
    """
    Compare each point with what the same time of day looked like on earlier
    days. This is the idea behind CloudWatch's anomaly band, though theirs is
    a trained model and this is a simple stand-in.
    """
    v = np.asarray(values, dtype=float)
    s = pd.Series(v)
    ts = pd.DatetimeIndex(timestamps)
    slot = np.asarray(ts.hour * 12 + ts.minute // 5)
    # lay the series out as a table: one row per time-of-day slot, one column
    # per earlier visit to that slot. then "what did this slot look like before"
    # is just the columns to the left.
    nth = pd.Series(slot).groupby(slot).cumcount().to_numpy()
    table = np.full((288, nth.max() + 1), np.nan)
    table[slot, nth] = v
    med = np.full(len(v), np.nan)
    spread = np.full(len(v), np.nan)
    for k in range(3, table.shape[1]):
        rows = np.flatnonzero(nth == k)
        past = table[slot[rows], :k]
        # the nan-aware version is much slower, only needed when the grid has holes
        pct = np.nanpercentile if np.isnan(past).any() else np.percentile
        q25, q50, q75 = pct(past, [25, 50, 75], axis=1)
        med[rows] = q50
        spread[rows] = (q75 - q25) / 1.349
    med, spread = pd.Series(med), pd.Series(spread)
    # with few days of history the spread can be tiny, so floor it
    floor = 0.25 * s.shift(1).expanding(min_periods=24).std()
    spread = np.maximum(spread, floor).replace(0, np.nan)
    score = ((s - med).abs() / spread)
    # no seasonal history yet for the first days: fall back to the z-score
    z = pd.Series(rolling_zscore(values))
    return score.fillna(z).fillna(0).to_numpy()


_forests = {}


def _features(values):
    s = pd.Series(np.asarray(values, dtype=float))
    return np.c_[
        s,
        s.rolling(12, min_periods=1).mean(),
        s.rolling(12, min_periods=2).std().fillna(0),
        s.diff().fillna(0),
    ]


def isolation_forest(values, timestamps=None, n_warm=0):
    # same four features the original NeuroOps model used, trained on the warm-up only
    X = _features(values)
    n_warm = max(n_warm, 50)
    # the model only ever sees the warm-up, so the same warm-up means the same
    # model. keeping it saves refitting every time the engine rescoring runs.
    key = (n_warm, hash(X[:n_warm].tobytes()))
    model = _forests.get(key)
    if model is None:
        if len(_forests) > 256:
            _forests.clear()
        model = _forests[key] = IsolationForest(n_estimators=100, random_state=0).fit(X[:n_warm])
    return -model.score_samples(X)


def _rescale(score, n_warm):
    # put scores from different detectors on one scale: 0 = typical, 1 = the
    # edge of what the warm-up period ever produced
    ref = score[min(50, n_warm // 2):max(n_warm, 51)]
    mid, top = np.median(ref), np.quantile(ref, 0.99)
    return (score - mid) / max(top - mid, 1e-9)


def _parts(values, timestamps, n_warm):
    z = _rescale(rolling_zscore(values), n_warm)
    f = _rescale(isolation_forest(values, n_warm=n_warm), n_warm)
    return z, f


def hybrid_and(values, timestamps=None, n_warm=0):
    # the original NeuroOps rule: only alert when both detectors agree
    z, f = _parts(values, timestamps, n_warm)
    return np.minimum(z, f)


def hybrid_mean(values, timestamps=None, n_warm=0):
    # average the two, so a very strong signal from one can still alert
    z, f = _parts(values, timestamps, n_warm)
    return (z + f) / 2


def hybrid_seasonal(values, timestamps, n_warm=0):
    # z-score + isolation forest + time-of-day band
    z, f = _parts(values, timestamps, n_warm)
    sb = _rescale(seasonal_band(values, timestamps), n_warm)
    return (z + f + sb) / 3


DETECTORS = {
    "static_threshold": static_threshold,
    "rolling_zscore": rolling_zscore,
    "ewma": ewma,
    "seasonal_band": seasonal_band,
    "isolation_forest": isolation_forest,
    "hybrid_and": hybrid_and,
    "hybrid_mean": hybrid_mean,
    "hybrid_seasonal": hybrid_seasonal,
}
