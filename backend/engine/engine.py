"""
The loop: metrics -> signals -> diagnosis -> decision -> action -> verify.

Every step is written to the audit log with its reason, including the things
the engine decided NOT to do.
"""
from datetime import timedelta

import pandas as pd

from backend.data.loaders import KIND
from backend.detect.detectors import DETECTORS, prepare
from backend.engine import policy as P
from backend.engine.diagnose import diagnose
from backend.engine.model import Decision, Incident
from backend.engine.notify import build_alert
from backend.logs.cluster import rising, recent_change
from backend.engine.signals import THRESHOLDS, anomaly_signal, forecast_signal, limit_signal

STEP = timedelta(minutes=5)
CONFIRM = {"critical": 2, "warning": 3}   # ticks in a row before a signal counts
VERIFY_AFTER = 3                          # ticks to wait before checking an action worked
CLEAR_AFTER = 6                           # quiet ticks before an incident closes


class Engine:
    def __init__(self, source, executor, config=None, warm_steps=864, auto_approve_after=None, notifier=None,
                 use_logs=True, dashboard_url=None):
        self.source = source
        self.executor = executor
        self.cfg = config or P.PolicyConfig()
        self.state = P.PolicyState()
        self.warm = warm_steps
        self.auto_approve_after = auto_approve_after   # only used by evaluations
        self.detector = DETECTORS[THRESHOLDS["detector"]]
        self.instances = source.instances()
        self.role = {i.id: i.role for i in self.instances}
        self.streak = {}
        self.incidents = []
        self.open = {}            # (cause, target) -> Incident
        self.verifications = []
        self.approvals = []
        self.audit = []
        self.notifier = notifier
        self.use_logs = use_logs
        self.dashboard_url = dashboard_url
        self.sink = None          # optional callable(event dict), used to keep the log in a database
        self._scores = {}
        self._said = {}

    # ---- bookkeeping ----
    def log(self, ts, kind, **data):
        event = {"ts": pd.Timestamp(ts).isoformat(), "type": kind, "simulated": self.source.simulated, **data}
        self.audit.append(event)
        if self.sink:
            try:
                self.sink(event)
            except Exception as e:   # a full disk must not stop the healing loop
                print(f"could not store audit event: {e}")

    def _logs_for(self, inc, now):
        """log lines that are new or louder in the last 30 minutes than in the day before"""
        if not self.use_logs or not hasattr(self.source, "logs"):
            return []
        try:
            w0 = now - 6 * STEP
            b0 = w0 - timedelta(hours=24)
            win, base = [], []
            for iid in self._members(inc.target):
                win += self.source.logs(iid, w0, now + STEP)
                base += self.source.logs(iid, b0, w0)
            if not win:
                return []
            scale = (now + STEP - w0) / (w0 - b0)
            return [{**c, "example": c["example"][:160]} for c in rising(win, base, top=4, baseline_scale=scale)]
        except Exception as e:   # a log source being down must not stop the healing loop
            print(f"log lookup failed: {e}")
            return []

    def _change_for(self, inc, now):
        """was something deployed in the two hours before this started? advice for the reader, it changes no decision."""
        if not self.use_logs or not hasattr(self.source, "logs"):
            return None
        try:
            lines = []
            for iid in self._members(inc.target):
                lines += self.source.logs(iid, inc.opened - timedelta(hours=2), now + STEP)
            return recent_change(lines, inc.opened)
        except Exception as e:
            print(f"deploy lookup failed: {e}")
            return None

    def _alert(self, now, inc, kind, why, resolved=False):
        if not self.notifier:
            return
        trail = [e for e in self.audit if e.get("incident") == inc.id]
        alert = build_alert(inc, why, trail, now, self.source.simulated, self.dashboard_url, resolved)
        for r in self.notifier.notify(alert, kind, now):
            self.log(now, "notified", incident=inc.id, why=kind, channel=r["channel"], ok=r["ok"],
                     subject=alert["subject"], text=alert["text"], error=r.get("error"))

    def _members(self, target):
        return [i.id for i in self.instances if i.role == target] or [target]

    def _invalidate(self, target):
        for iid in self._members(target):
            for key in [k for k in self._scores if k[0] == iid]:
                del self._scores[key]

    def _score(self, iid, metric, step):
        # detectors only look backwards, so one pass over the whole replay gives
        # the same numbers as scoring point by point. redone after any action.
        key = (iid, metric)
        if key not in self._scores:
            self._scores[key] = self.detector(prepare(self.source.full(iid, metric), KIND.get(metric)), self.source.ts, self.warm)
        return self._scores[key][step]

    # ---- one tick ----
    def step(self, step):
        now = self.source.ts[step].to_pydatetime()
        signals = []
        for inst in self.instances:
            for metric in self.source.metrics(inst.id):
                _, values = self.source.history(inst.id, metric, step)
                found = [limit_signal(now, inst.id, metric, values[-1]),
                         forecast_signal(now, inst.id, metric, values)]
                if metric != "CPUCreditBalance":
                    found.append(anomaly_signal(now, inst.id, metric, values, self._score(inst.id, metric, step)))
                for s in found:
                    key = (inst.id, metric, s.kind if s else None)
                    if s is None:
                        continue
                    self.streak[key] = self.streak.get(key, (0, step - 1))
                    count, last = self.streak[key]
                    count = count + 1 if last == step - 1 else 1
                    self.streak[key] = (count, step)
                    need = 1 if s.kind == "forecast" else CONFIRM[s.severity]
                    if count >= need:
                        signals.append(s)
        return self._handle(now, step, signals)

    def _handle(self, now, step, signals):
        diags = diagnose(signals, self.instances)
        active = set()
        for d in diags:
            key = (d.cause, d.target)
            active.add(key)
            inc = self.open.get(key)
            if inc is None:
                inc = Incident(len(self.incidents) + 1, d.cause, d.scope, d.target, now, d.severity, evidence=d.evidence[:4])
                inc.logs = self._logs_for(inc, now)
                inc.change = self._change_for(inc, now)
                self.incidents.append(inc)
                self.open[key] = inc
                self.log(now, "incident_opened", incident=inc.id, cause=d.cause, target=d.target,
                         severity=d.severity, evidence=d.evidence[:4])
            inc.last_seen = now
            if d.severity == "critical":
                inc.severity = "critical"

        self._verify(now, step, active)
        self._approvals(now, step)

        for key in sorted(active):
            inc = self.open[key]
            if inc.status != "open" or self._waiting(inc):
                continue
            action, status, reasons = P.decide(inc, now, self.instances, self.cfg, self.state)
            said = (action, status)
            if self._said.get(inc.id) == said:
                continue   # same answer as last tick, no need to log it again
            self._said[inc.id] = said
            dec = Decision(now, inc.id, inc.cause, action, inc.target, status, reasons)
            self.log(now, "decision", incident=inc.id, cause=dec.cause, action=dec.action,
                     target=dec.target, status=dec.status, reasons=dec.reasons)
            if status == "shadow" and inc.severity == "critical":
                self._alert(now, inc, "shadow", ["shadow mode: the engine would have acted but is only watching"])
            if status == "executed":
                self._execute(now, step, inc, action)
            elif status == "needs_approval":
                self.approvals.append(dict(id=len(self.approvals) + 1, incident=inc.id, action=action, target=inc.target,
                                           reasons=reasons, requested=now, step=step, status="pending"))
                inc.logs = self._logs_for(inc, now) or inc.logs
                self._alert(now, inc, "approval", [f"waiting for approval to {action}"] + reasons)
            elif status == "notify":
                inc.status = "escalated"
                self.log(now, "escalated", incident=inc.id, cause=inc.cause, target=inc.target, reasons=reasons)
                inc.logs = self._logs_for(inc, now) or inc.logs
                self._alert(now, inc, "escalated", reasons)

        self._close(now, step, active)
        return signals, diags

    def _waiting(self, inc):
        return any(v["incident"] is inc for v in self.verifications) or \
            any(a["incident"] == inc.id and a["status"] == "pending" for a in self.approvals)

    def _execute(self, now, step, inc, action, approved_by=None):
        res = self.executor.run(action, inc.target, {"step": step, "now": now})
        inc.attempts[action] = inc.attempts.get(action, 0) + 1
        self.log(now, "action", incident=inc.id, action=action, target=inc.target, ok=res["ok"],
                 detail=res["detail"], approved_by=approved_by)
        if not res["ok"]:
            self._said.pop(inc.id, None)
            return
        spec = P.ACTIONS[action]
        tier = inc.target if inc.scope == "tier" else self.role.get(inc.target)
        self.state.last_action[inc.target] = now
        if approved_by is None:
            self.state.auto_times.append(now)
        if spec["disruptive"]:
            self.state.acting_in_tier[tier] = (inc.target, now + VERIFY_AFTER * STEP)
        if action == "scale_out":
            self.state.scaled[tier] = self.state.scaled.get(tier, 0) + 1
        self._invalidate(inc.target)
        self.verifications.append(dict(incident=inc, action=action, due=step + VERIFY_AFTER + 1, undo=res["undo"]))

    def _verify(self, now, step, active):
        for v in [v for v in self.verifications if v["due"] <= step]:
            self.verifications.remove(v)
            inc = v["incident"]
            key = (inc.cause, inc.target)
            # a config change like unlimited credits works by definition, the balance just takes time to refill
            worked = key not in active or v["action"] == "enable_unlimited"
            if v["undo"]:
                inc.__dict__.setdefault("undo_stack", []).append((v["action"], v["undo"]))
            if worked:
                inc.status = "mitigated"
                self.log(now, "verified", incident=inc.id, action=v["action"], target=inc.target,
                         detail="the problem signal is gone")
            else:
                # did not help: forget the cooldown so the next rung of the ladder can run
                self.state.last_action.pop(inc.target, None)
                self._said.pop(inc.id, None)
                self.log(now, "verify_failed", incident=inc.id, action=v["action"], target=inc.target,
                         detail="the problem signal is still there, moving to the next step")

    def _approvals(self, now, step):
        for a in self.approvals:
            if a["status"] != "pending":
                continue
            inc = self.incidents[a["incident"] - 1]
            if inc.status in ("resolved",):
                a["status"] = "expired"
                continue
            if self.auto_approve_after is not None and step - a["step"] >= self.auto_approve_after:
                self.approve(a["id"], now, step, by="evaluation-auto-approver")

    def approve(self, approval_id, now, step, by="operator"):
        a = self.approvals[approval_id - 1]
        if a["status"] != "pending":
            return False
        a["status"] = "approved"
        self.log(now, "approved", incident=a["incident"], action=a["action"], target=a["target"], by=by)
        self._execute(now, step, self.incidents[a["incident"] - 1], a["action"], approved_by=by)
        return True

    def reject(self, approval_id, now, by="operator"):
        a = self.approvals[approval_id - 1]
        if a["status"] != "pending":
            return False
        a["status"] = "rejected"
        inc = self.incidents[a["incident"] - 1]
        inc.attempts[a["action"]] = inc.attempts.get(a["action"], 0) + 1
        self._said.pop(inc.id, None)
        self.log(now, "rejected", incident=a["incident"], action=a["action"], target=a["target"], by=by)
        return True

    def _close(self, now, step, active):
        for key, inc in list(self.open.items()):
            if key in active:
                # a fix that held for a while and then stopped holding: reopen it
                if inc.status == "mitigated" and not self._waiting(inc) and \
                        now - self.state.last_action.get(inc.target, now) > 2 * self.cfg.cooldown \
                        and "enable_unlimited" not in inc.attempts:
                    inc.status = "open"
                    self._said.pop(inc.id, None)
                    self.log(now, "reopened", incident=inc.id, cause=inc.cause, target=inc.target)
                continue
            if self._waiting(inc) and any(v["incident"] is inc for v in self.verifications):
                continue
            if now - inc.last_seen >= CLEAR_AFTER * STEP:
                how = "after action" if any(k in P.ACTIONS for k in inc.attempts) and inc.status == "mitigated" else "on its own"
                inc.status, inc.closed = "resolved", now
                del self.open[key]
                # temporary changes go back once the trouble is over
                for action, undo in reversed(inc.__dict__.get("undo_stack", [])):
                    res = self.executor.undo(action, inc.target, undo, {"step": step, "now": now})
                    if action == "scale_out" and res["ok"]:
                        tier = inc.target
                        self.state.scaled[tier] = max(0, self.state.scaled.get(tier, 0) - 1)
                    self._invalidate(inc.target)
                    self.log(now, "rolled_back", incident=inc.id, action=action, target=inc.target,
                             ok=res["ok"], detail=res["detail"])
                self.log(now, "incident_resolved", incident=inc.id, cause=inc.cause, target=inc.target, how=how)
                if self.notifier and any(k[0] == inc.id for k in self.notifier.done):
                    self._alert(now, inc, "resolved", [], resolved=True)

    def run(self, start=None, end=None):
        start = self.warm if start is None else start
        end = len(self.source.ts) if end is None else end
        for step in range(start, end):
            self.step(step)
        return self
