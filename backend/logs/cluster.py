"""
Groups log lines that are the same message with different numbers, ids and
hosts in them, so 4000 lines read as 6 kinds of line with counts.

This is a small version of the Drain idea: mask the obviously variable
tokens, bucket by length and first word, then merge lines that share most of
their words and mark the words that differ as <*>. It is checked against the
hand-labelled Loghub samples in eval/logs_eval.py.
"""
import re
from dataclasses import dataclass, field

WILD = "<*>"
LEVELS = ("fatal", "critical", "error", "err", "warn", "warning", "notice", "info", "debug")

# order matters: the longer shapes go first
_MASKS = [
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),   # uuid
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),                                       # ip and port
    re.compile(r"\b0x[0-9a-f]+\b", re.I),
    re.compile(r"\b[0-9a-f]{12,}\b", re.I),                                                    # long hex ids
    re.compile(r"\bi-[0-9a-f]{8,17}\b"),                                                       # ec2 ids
]


def level_of(line):
    low = line.lower()
    for lv in LEVELS:
        if re.search(rf"\b{lv}\b", low[:60]):
            return {"err": "error", "warning": "warn", "critical": "fatal"}.get(lv, lv)
    return "info"


def tokens(line):
    s = line.strip()
    for m in _MASKS:
        s = m.sub(WILD, s)
    out = []
    for t in s.split():
        # anything with a digit in it is almost always a value, not a word
        out.append(WILD if any(c.isdigit() for c in t) else t)
    return out


@dataclass
class Cluster:
    template: list
    count: int = 0
    first: object = None
    last: object = None
    example: str = ""
    level: str = "info"
    members: list = field(default_factory=list)   # line numbers, only kept when asked

    @property
    def text(self):
        return " ".join(self.template)


class Clusterer:
    def __init__(self, similarity=0.5, keep_members=False):
        self.sim = similarity
        self.keep = keep_members
        self.buckets = {}     # (length, first word) -> [Cluster]
        self.clusters = []

    def add(self, line, ts=None, index=None):
        toks = tokens(line)
        if not toks:
            return None
        key = (len(toks), toks[0] if toks[0] != WILD else "")
        best, best_score = None, 0.0
        for c in self.buckets.setdefault(key, []):
            same = sum(1 for a, b in zip(c.template, toks) if a == b and a != WILD)
            fixed = sum(1 for a in c.template if a != WILD) or 1
            score = same / max(fixed, sum(1 for b in toks if b != WILD) or 1)
            if score > best_score:
                best, best_score = c, score
        if best is None or best_score < self.sim:
            best = Cluster(template=list(toks), example=line.strip(), level=level_of(line), first=ts)
            self.buckets[key].append(best)
            self.clusters.append(best)
        else:
            best.template = [a if a == b else WILD for a, b in zip(best.template, toks)]
        best.count += 1
        best.last = ts
        if best.first is None:
            best.first = ts
        if self.keep and index is not None:
            best.members.append(index)
        return best


def cluster_lines(lines, timestamps=None, **kw):
    cl = Clusterer(**kw)
    for i, line in enumerate(lines):
        cl.add(line, None if timestamps is None else timestamps[i], i)
    return cl.clusters


def rising(window_lines, baseline_lines, top=5, baseline_scale=1.0, min_ratio=3.0):
    """
    What is new or louder in the window than in the baseline. Both sets are
    clustered together so one message shape has one name, then each cluster is
    ranked by how much more often it appears in the window than expected.
    `baseline_scale` is window length divided by baseline length. Clusters that
    are not at least `min_ratio` times louder than usual are left out, so
    ordinary traffic never shows up as news.
    """
    cl = Clusterer()
    win, base = {}, {}
    for ts, line in baseline_lines:
        c = cl.add(line, ts)
        if c is not None:
            base[id(c)] = base.get(id(c), 0) + 1
    for ts, line in window_lines:
        c = cl.add(line, ts)
        if c is not None:
            win[id(c)] = win.get(id(c), 0) + 1
            c.example = line.strip()
    out = []
    for c in cl.clusters:
        w, b = win.get(id(c), 0), base.get(id(c), 0)
        if not w:
            continue
        expected = b * baseline_scale
        # +1 so a line that was never seen before ranks by how often it shows up now
        ratio = w / (expected + 1.0)
        weight = {"fatal": 3, "error": 2, "warn": 1.4}.get(c.level, 1.0)
        if ratio * weight < min_ratio:
            continue
        out.append(dict(template=c.text, example=c.example, level=c.level, in_window=w, in_baseline=b,
                        new=b == 0, score=round(ratio * weight, 2)))
    out.sort(key=lambda x: -x["score"])
    return out[:top]
