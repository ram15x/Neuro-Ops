"""
API for the engine. In demo mode the engine runs on the simulated fleet and
the clock moves faster than real time.
"""
import threading

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.auth import get_current_user
from backend.core.config import settings
from backend.engine.engine import Engine
from backend.engine.executors import SimExecutor
from backend.engine.export import snapshot
from backend.engine.model import as_dict
from backend.engine.notify import from_settings
from backend.engine.policy import PolicyConfig
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
        self.store = settings.ENGINE_STORE_EVENTS
        self.archive = None
        if settings.ARCHIVE_S3_BUCKET:
            from backend.storage.archive import S3Archive
            self.archive = S3Archive(settings.ARCHIVE_S3_BUCKET, settings.AWS_REGION)
        self.archived = 0
        self._new_run()

    def _new_run(self):
        fleet = build_fleet(seed=self.seed)
        mode = getattr(self, "engine", None).cfg.mode if getattr(self, "engine", None) else settings.ENGINE_MODE
        self.fleet = fleet
        self.engine = Engine(SimSource(fleet), SimExecutor(fleet), PolicyConfig(mode=mode),
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


_runner = None


def get_runner():
    global _runner
    if _runner is None:
        _runner = Runner()
    return _runner


class ModeIn(BaseModel):
    mode: str


@router.get("/engine/status")
def status():
    r = get_runner()
    with r.lock:
        e = r.engine
        return {
            "simulated": e.source.simulated, "source": "simulated fleet", "mode": e.cfg.mode,
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
    with r.lock:
        points = max(12, min(points, 2016))
        out = snapshot(r.fleet, r.engine, max(0, r.step - points), r.step, "live")
        out["meta"]["live"] = True
        return out


@router.get("/engine/fleet")
def fleet():
    r = get_runner()
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


@router.get("/engine/approvals")
def approvals():
    r = get_runner()
    with r.lock:
        rows = [{**a, "requested": a["requested"].isoformat()} for a in r.engine.approvals]
        return {"simulated": True, "approvals": rows[::-1]}


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
    with r.lock:
        old = r.engine.cfg.mode
        r.engine.cfg.mode = body.mode
        r.engine._said.clear()
        r.engine.log(r.now(), "mode_changed", old=old, new=body.mode, by=user.username)
        return {"mode": body.mode}
