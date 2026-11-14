"""
Checks the log grouping against Loghub, the public hand-labelled log samples
that the Drain paper used (2000 lines each, real systems, not mine).

Metric: grouping accuracy. A line counts as right only if its group holds
exactly the same lines as its true group, which is strict.

Run:  python -m eval.logs_eval [folder with *_2k.log_structured.csv]
The csv files are not in the repo (licence is for research), the script
downloads them from github.com/logpai/loghub when the folder is empty.
"""
import csv
import os
import sys
import urllib.request

from backend.logs.cluster import Clusterer

SETS = ["Apache", "HDFS", "Linux", "OpenSSH"]


def fetch(folder):
    os.makedirs(folder, exist_ok=True)
    for n in SETS:
        path = os.path.join(folder, f"{n}_2k.log_structured.csv")
        if not os.path.exists(path):
            url = f"https://raw.githubusercontent.com/logpai/loghub/master/{n}/{n}_2k.log_structured.csv"
            urllib.request.urlretrieve(url, path)


def grouping_accuracy(truth, pred):
    by_t, by_p = {}, {}
    for i, (t, p) in enumerate(zip(truth, pred)):
        by_t.setdefault(t, set()).add(i)
        by_p.setdefault(p, set()).add(i)
    ok = sum(len(s) for s in by_t.values() if any(s == q for q in by_p.values()))
    return ok / len(truth)


def score(folder, similarity=0.5):
    out = {}
    for n in SETS:
        rows = list(csv.DictReader(open(os.path.join(folder, f"{n}_2k.log_structured.csv"), encoding="utf-8")))
        cl = Clusterer(similarity=similarity, keep_members=True)
        pred = [None] * len(rows)
        for i, r in enumerate(rows):
            pred[i] = id(cl.add(r["Content"], index=i))
        out[n] = dict(lines=len(rows), true_templates=len({r["EventTemplate"] for r in rows}),
                      found=len(cl.clusters), accuracy=round(grouping_accuracy([r["EventTemplate"] for r in rows], pred), 3))
    return out


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "/tmp/loghub"
    fetch(folder)
    res = score(folder)
    for n, r in res.items():
        print(f"{n:8} lines={r['lines']} true groups={r['true_templates']:3} found={r['found']:3} grouping accuracy={r['accuracy']}")
    print("mean", round(sum(r["accuracy"] for r in res.values()) / len(res), 3))
