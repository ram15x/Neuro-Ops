"""
API for the engine. In demo mode the engine runs on the simulated fleet and
the clock moves faster than real time.
"""
import copy
import os
import threading
import time

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from backend.api.auth import get_current_user
from backend.core.config import settings
from backend.engine.engine import Engine
from backend.engine.executors import SimExecutor
from backend.engine.export import snapshot
from backend.engine.model import as_dict
from backend.engine.metrics import render as render_metrics
from backend.engine.notify import from_settings
from backend.engine.report import postmortem, reliability
from backend.engine.policy import PolicyConfig
from backend.engine.policy_file import load as load_policy
from backend.engine.sources import SimSource
from backend.sim.aws_fleet import build_fleet

router = APIRouter()


class Runner:
    """Owns one engine and moves it forward one datapoint at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()
        self.seed = settings.ENGINE_SEED
        self.policy = self._policy()
        self.store = settings.ENGINE_STORE_EVENTS
        self.archive = None
        if settings.ARCHIVE_S3_BUCKET:
            from backend.storage.archive import S3Archive
            self.archive = S3Archive(settings.ARCHIVE_S3_BUCKET, settings.AWS_REGION)
        self.archived = 0
        self.last_tick, self.ticks = 0.0, 0
        self._new_run()

    @staticmethod
    def _policy():
        """the policy file, with the safer of its mode and ENGINE_MODE winning. a bad file stops startup."""
        cfg = load_policy(settings.POLICY_FILE) if os.path.exists(settings.POLICY_FILE) else PolicyConfig()
        rank = {"shadow": 0, "approval": 1, "auto": 2}
        if rank.get(settings.ENGINE_MODE, 0) < rank[cfg.mode]:
            cfg.mode = settings.ENGINE_MODE
        return cfg

    def _new_run(self):
        fleet = build_fleet(seed=self.seed)
        cfg = copy.copy(self.policy)
        if getattr(self, "engine", None):
            cfg.mode = self.engine.cfg.mode   # a mode someone set through the API survives a new run
        self.fleet = fleet
        self.engine = Engine(SimSource(fleet), SimExecutor(fleet), cfg,
                             notifier=from_settings(settings, simulated=True), dashboard_url=settings.ALERT_DASHBOARD_URL or None)
        if self.store:
            self.engine.sink = self._store_event
        self.step = self.engine.warm

    def _store_event(self, e):
        from backend.models.database import EngineEvent, SessionLocal
        db = SessionLocal()
        try:
            rest = {k: v for k, v in e.items() if k not in ("ts", "type", "simulated", "incident")}
            db.add(EngineEvent(run=self.seed, at=e["ts"], kind=e["type"], incident=e.get("incident"),
                               simulated=e["simulated"], payload=rest))
            db.commit()
        finally:
            db.close()

    def _flush_archive(self):
        # once per simulated day, and when a run ends, copy the new audit lines to S3
        if not self.archive:
            return
        fresh = self.engine.audit[self.archived:]
        if fresh:
            try:
                self.archive.put(self.seed, fresh)
                self.archived += len(fresh)
            except Exception as e:   # S3 being down must not stop the engine, it retries next time
                print(f"archive failed: {e}")

    def tick(self):
        t0 = time.perf_counter()
        try:
            self._tick()
        finally:
            self.last_tick = time.perf_counter() - t0
            self.ticks += 1

    def _tick(self):
        with self.lock:
            if self.step % 288 == 0 or self.step >= self.fleet.n:
                self._flush_archive()
            if self.step >= self.fleet.n:
                # end of the simulated fortnight: start a fresh one
                self.seed += 1
                self.archived = 0
                self._new_run()
            self.engine.step(self.step)
            self.step += 1

    def start(self):
        if self.thread:
            return

        def loop():
            while not self.stop.wait(settings.ENGINE_TICK_SECONDS):
                try:
                    self.tick()
                except Exception as e:   # keep the loop alive, the error is in the log
                    print(f"engine tick failed: {e}")

        self.thread = threading.Thread(target=loop, daemon=True, name="engine")
        self.thread.start()

    def now(self):
        return self.fleet.ts[min(self.step, self.fleet.n) - 1]


class LiveRunner:
    """
    Same job as Runner but the data is real CloudWatch, read every 5 minutes.
    Stays in shadow unless ENGINE_LIVE_ACTIONS is on, whatever the mode setting says.
    """
    live = True

    def __init__(self, source=None, executor=None):
        from backend.engine.executors import AwsExecutor
        from backend.engine.sources import CloudWatchSource
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()
        self.seed = 0
        self.policy = Runner._policy()
        if not settings.ENGINE_LIVE_ACTIONS:
            self.policy.mode = "shadow"
        self.store = settings.ENGINE_STORE_EVENTS
        self.last_tick, self.ticks = 0.0, 0
        self.step = 0
        self.data = {}
        self.clock = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
        src = source or CloudWatchSource(settings.AWS_REGION, lookback_days=settings.ENGINE_WARM_DAYS)
        ex = executor or AwsExecutor(settings.AWS_REGION)
        self.engine = Engine(src, ex, self.policy, notifier=from_settings(settings, simulated=False),
                             dashboard_url=settings.ALERT_DASHBOARD_URL or None)
        if self.store:
            self._restore()
            self.engine.sink = self._store_event

    _store_event = Runner._store_event

    def _restore(self):
        """after a restart, reload this run's audit lines so open incidents carry on instead of starting over"""
        from backend.models.database import EngineEvent, SessionLocal
        db = SessionLocal()
        try:
            rows = db.query(EngineEvent).filter(EngineEvent.run == self.seed, EngineEvent.simulated.is_(False)) \
                .order_by(EngineEvent.id).all()
            events = [{"ts": r.at, "type": r.kind, "simulated": False, "incident": r.incident, **(r.payload or {})} for r in rows]
        finally:
            db.close()
        if events:
            n = self.engine.restore(events)
            print(f"restored {len(events)} audit lines, {n} open incidents")

    def now(self):
        return self.clock

    def tick(self):
        t0 = time.perf_counter()
        try:
            with self.lock:
                self.clock = pd.Timestamp.now("UTC").tz_localize(None).floor("5min")
                self.engine.step_live(self.clock.to_pydatetime(), self.step)
                self.data = dict(self.engine._live)
                self.step += 1
        finally:
            self.last_tick = time.perf_counter() - t0
            self.ticks += 1

    def start(self):
        if self.thread:
            return

        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:   # a bad tick must not kill the loop
                    print(f"engine tick failed: {e}")
                if self.stop.wait(300):
                    return

        self.thread = threading.Thread(target=loop, daemon=True, name="engine-live")
        self.thread.start()


_runner = None


def get_runner():
    global _runner
    if _runner is None:
        _runner = LiveRunner() if settings.ENGINE_SOURCE == "cloudwatch" else Runner()
    return _runner


class ModeIn(BaseModel):
    mode: str


@router.get("/engine/status")
def status():
    r = get_runner()
    with r.lock:
        e = r.engine
        return {
            "simulated": e.source.simulated, "source": "cloudwatch" if getattr(r, "live", False) else "simulated fleet", "mode": e.cfg.mode,
            "now": r.now().isoformat(), "step": r.step, "seed": r.seed,
            "open_incidents": len(e.open),
            "pending_approvals": sum(a["status"] == "pending" for a in e.approvals),
            "incidents_total": len(e.incidents),
            "actions_total": sum(a["type"] == "action" for a in e.audit),
        }


@router.get("/engine/snapshot")
def snap(points: int = 576):
    """the same shape as a recording, covering the last `points` datapoints"""
    r = get_runner()
    if getattr(r, "live", False):
        raise HTTPException(501, "the replay snapshot is only built for the simulated fleet, use /engine/fleet and /engine/series")
    with r.lock:
        points = max(12, min(points, 2016))
        out = snapshot(r.fleet, r.engine, max(0, r.step - points), r.step, "live")
        out["meta"]["live"] = True
        return out


@router.get("/engine/fleet")
def fleet():
    r = get_runner()
    if getattr(r, "live", False):
        with r.lock:
            out = []
            for inst in r.engine.instances:
                latest = {m: round(float(v[-1]), 2) for (i, m), (_, v) in r.data.items() if i == inst.id}
                troubles = [as_dict(i) for i in r.engine.open.values() if i.target == inst.id or i.target == inst.role]
                out.append({"id": inst.id, "role": inst.role, "type": inst.itype, "managed": inst.managed,
                            "latest": latest, "incidents": troubles})
            return {"simulated": False, "now": r.now().isoformat(), "instances": out}
    with r.lock:
        out = []
        for inst in r.engine.instances:
            data = r.fleet.render(inst.id)
            latest = {m: round(float(v[r.step - 1]), 2) for m, v in data.items()}
            troubles = [as_dict(i) for i in r.engine.open.values()
                        if i.target == inst.id or i.target == inst.role]
            out.append({"id": inst.id, "role": inst.role, "type": inst.itype, "latest": latest, "incidents": troubles})
        return {"simulated": True, "now": r.now().isoformat(), "instances": out}


@router.get("/engine/series")
def series(instance_id: str, metric: str, points: int = 288):
    r = get_runner()
    if getattr(r, "live", False):
        with r.lock:
            got = r.data.get((instance_id, metric))
            if got is None:
                raise HTTPException(404, "no data for this instance and metric")
            points = max(1, min(points, 2016))
            ts, vals = got
            return {"simulated": False, "instance_id": instance_id, "metric": metric,
                    "timestamps": [t.isoformat() for t in ts[-points:]],
                    "values": np.round(vals[-points:], 3).tolist()}
    with r.lock:
        if instance_id not in r.fleet.instances:
            raise HTTPException(404, "unknown instance")
        data = r.fleet.render(instance_id)
        if metric not in data:
            raise HTTPException(404, "this instance has no such metric")
        points = max(1, min(points, 2016))
        lo = max(0, r.step - points)
        return {"simulated": True, "instance_id": instance_id, "metric": metric,
                "timestamps": [t.isoformat() for t in r.fleet.ts[lo:r.step]],
                "values": np.round(data[metric][lo:r.step], 3).tolist()}


@router.get("/engine/incidents")
def incidents(limit: int = 50):
    r = get_runner()
    with r.lock:
        return {"simulated": True, "incidents": [as_dict(i) for i in r.engine.incidents[-limit:]][::-1]}


@router.get("/engine/audit")
def audit(limit: int = 100):
    r = get_runner()
    with r.lock:
        return {"simulated": True, "events": r.engine.audit[-limit:][::-1]}


@router.get("/engine/history")
def history(limit: int = 200, run: int = None):
    """the audit log as stored in the database, so it is still here after a restart"""
    from backend.models.database import EngineEvent, SessionLocal
    db = SessionLocal()
    try:
        q = db.query(EngineEvent)
        if run is not None:
            q = q.filter(EngineEvent.run == run)
        rows = q.order_by(EngineEvent.id.desc()).limit(max(1, min(limit, 1000))).all()
        return {"events": [{"id": x.id, "run": x.run, "at": x.at, "type": x.kind, "incident": x.incident,
                            "simulated": x.simulated, **(x.payload or {})} for x in rows]}
    finally:
        db.close()


@router.get("/engine/report")
def report():
    """how long incidents stayed open, how fast the engine acted, what it fixed on its own"""
    r = get_runner()
    with r.lock:
        return {**reliability(r.engine, r.now().to_pydatetime())}


@router.get("/engine/incidents/{incident_id}/postmortem", response_class=PlainTextResponse)
def incident_postmortem(incident_id: int):
    """a markdown draft, built from the audit log"""
    r = get_runner()
    with r.lock:
        if not 1 <= incident_id <= len(r.engine.incidents):
            raise HTTPException(404, "unknown incident")
        return postmortem(r.engine, r.engine.incidents[incident_id - 1], r.now().to_pydatetime())


def metrics_text():
    r = get_runner()
    with r.lock:
        return render_metrics(r.engine, r.step, r.last_tick, r.ticks)


@router.get("/engine/approvals")
def approvals():
    r = get_runner()
    with r.lock:
        rows = [{**a, "requested": a["requested"].isoformat()} for a in r.engine.approvals]
        return {"simulated": not getattr(r, "live", False), "approvals": rows[::-1]}


@router.post("/engine/approvals/{approval_id}/{verdict}")
def answer(approval_id: int, verdict: str, user=Depends(get_current_user)):
    r = get_runner()
    if verdict not in ("approve", "reject"):
        raise HTTPException(400, "verdict must be approve or reject")
    with r.lock:
        if not 1 <= approval_id <= len(r.engine.approvals):
            raise HTTPException(404, "unknown approval")
        now = r.now().to_pydatetime()
        if verdict == "approve":
            ok = r.engine.approve(approval_id, now, r.step - 1, by=user.username)
        else:
            ok = r.engine.reject(approval_id, now, by=user.username)
        if not ok:
            raise HTTPException(409, "this request was already answered or has expired")
        return {"ok": True}


@router.post("/engine/mode")
def set_mode(body: ModeIn, user=Depends(get_current_user)):
    if body.mode not in ("shadow", "approval", "auto"):
        raise HTTPException(400, "mode must be shadow, approval or auto")
    r = get_runner()
    if getattr(r, "live", False) and body.mode != "shadow" and not settings.ENGINE_LIVE_ACTIONS:
        raise HTTPException(403, "live actions are switched off (ENGINE_LIVE_ACTIONS), the engine stays in shadow")
    with r.lock:
        old = r.engine.cfg.mode
        r.engine.cfg.mode = body.mode
        r.engine._said.clear()
        r.engine.log(r.now(), "mode_changed", old=old, new=body.mode, by=user.username)
        return {"mode": body.mode}
