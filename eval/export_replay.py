"""
Records the engine running on the simulated fleet, for the hosted demo.

Writes four recordings to dashboard/replay/: clean and hard faults, each in
shadow mode (nothing executed) and auto mode (a stand-in operator approves
requests after 15 minutes).

Run:  python -m eval.export_replay
"""
import json
import os

from backend.data.loaders import ROOT
from backend.engine.engine import Engine
from backend.engine.executors import SimExecutor
from backend.engine.export import snapshot
from backend.engine.policy import PolicyConfig
from backend.engine.sources import SimSource
from backend.sim.aws_fleet import build_fleet
from eval.closed_loop import _score

OUT = os.path.join(ROOT, "dashboard", "replay")
SEEDS = {"clean": 21, "hard": 21}
LEAD_IN = 288   # show one quiet day before the engine starts watching


def record(scenario, mode):
    fleet = build_fleet(seed=SEEDS[scenario], hard=scenario == "hard")
    eng = Engine(SimSource(fleet), SimExecutor(fleet), PolicyConfig(mode=mode),
                 auto_approve_after=3 if mode == "auto" else None).run()
    snap = snapshot(fleet, eng, eng.warm - LEAD_IN, fleet.n, scenario)
    snap["meta"]["watching_from"] = LEAD_IN
    snap["meta"]["operator"] = "stand-in operator approves after 15 minutes" if mode == "auto" else None
    score = _score(fleet, eng)
    snap["summary"] = {k: v for k, v in score.items() if k != "by_kind"}
    return snap


def run():
    os.makedirs(OUT, exist_ok=True)
    index = []
    for scenario in ("clean", "hard"):
        for mode in ("shadow", "auto"):
            snap = record(scenario, mode)
            name = f"{scenario}-{mode}.json"
            json.dump(snap, open(os.path.join(OUT, name), "w"), separators=(",", ":"))
            index.append({"scenario": scenario, "mode": mode, "file": name})
            print(name, os.path.getsize(os.path.join(OUT, name)) // 1024, "KB", snap["summary"])
    json.dump({"simulated": True, "recordings": index}, open(os.path.join(OUT, "index.json"), "w"))


if __name__ == "__main__":
    run()
