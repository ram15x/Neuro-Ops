"""
Picks the alert threshold per metric kind for the default detector and writes
backend/detect/thresholds.json.

Calibration uses simulator seeds 1 to 3. The scorecard and the closed-loop
evaluation use seeds 7 to 9, so the engine is never graded on the data its
thresholds came from.

Run:  python -m eval.calibrate
"""
import json
import os

from backend.data.loaders import load_sim, ROOT
from eval.scorecard import _prepare, _pick_threshold, DEFAULT_DETECTOR, MAIN_BUDGET

SEEDS = (1, 2, 3)


def run():
    series = [s for seed in SEEDS for s in load_sim(seed=seed)]
    prepared = _prepare(series, [DEFAULT_DETECTOR])
    out = {}
    for kind in sorted({p["s"].kind for p in prepared}):
        group = [p for p in prepared if p["s"].kind == kind]
        out[kind] = round(float(_pick_threshold(group, DEFAULT_DETECTOR, MAIN_BUDGET)), 3)
    doc = {"detector": DEFAULT_DETECTOR, "budget_false_alarms_per_series_day": MAIN_BUDGET,
           "calibrated_on": f"simulator seeds {list(SEEDS)}", "thresholds": out}
    json.dump(doc, open(os.path.join(ROOT, "backend", "detect", "thresholds.json"), "w"), indent=1)
    return doc


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
