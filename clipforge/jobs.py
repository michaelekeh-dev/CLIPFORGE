"""Background jobs with progress. One small thread pool; the web app never blocks."""
from __future__ import annotations
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from . import db
from .config import cfg


class Progress:
    """Callback object passed to pipeline steps: progress(stage, pct)."""

    def __init__(self, job_id: str, target_kind: str, target_id: str):
        self.job_id = job_id
        self.kind = target_kind
        self.target_id = target_id
        self.cancelled = False

    def __call__(self, stage: str, pct: float | None = None, status: str | None = None):
        vals = {"stage": stage}
        if pct is not None:
            vals["progress"] = max(0.0, min(100.0, float(pct)))
        db.update("jobs", self.job_id, vals)
        tv = dict(vals)
        if status:
            tv["status"] = status
        db.update(self.kind, self.target_id, tv)


class JobRunner:
    def __init__(self, workers: int | None = None):
        self.workers = workers or int(cfg.get("app.workers", 1))
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="job")
        self.running: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, target_id: str, fn: Callable[[Progress], None]) -> str:
        job_id = db.new_id("job_")
        db.insert("jobs", {"id": job_id, "kind": kind, "target_id": target_id, "status": "queued", "owner_pid": os.getpid()})
        db.update(kind, target_id, {"status": "queued", "stage": "Waiting to start", "progress": 0, "error": ""})
        self.pool.submit(self._run, job_id, kind, target_id, fn)
        return job_id

    def _run(self, job_id, kind, target_id, fn):
        prog = Progress(job_id, kind, target_id)
        db.update("jobs", job_id, {"status": "running", "started_at": time.time()})
        db.update(kind, target_id, {"status": "running"})
        with self._lock:
            self.running[job_id] = threading.current_thread()
        try:
            fn(prog)
            db.update("jobs", job_id, {"status": "done", "progress": 100, "finished_at": time.time()})
            db.update(kind, target_id, {"status": "done", "progress": 100})
        except Exception as e:  # noqa: BLE001
            msg = friendly_error(e)
            db.log_error(f"{kind}:{target_id}", msg + "\n" + traceback.format_exc())
            db.update("jobs", job_id, {"status": "error", "error": msg, "finished_at": time.time()})
            db.update(kind, target_id, {"status": "error", "error": msg})
        finally:
            with self._lock:
                self.running.pop(job_id, None)

    def running_count(self) -> int:
        with self._lock:
            return len(self.running)


def recover_dead_jobs() -> int:
    """Jobs left running/queued by a process that no longer exists are marked as errors (with a retry hint)."""
    n = 0
    for j in db.rows("SELECT * FROM jobs WHERE status IN ('running','queued')"):
        pid = int(j.get("owner_pid") or 0)
        if pid and pid != os.getpid() and os.path.exists(f"/proc/{pid}"):
            continue  # another live server process owns it
        if pid == os.getpid():
            continue
        msg = "The app restarted while this was running. Press Try again."
        db.update("jobs", j["id"], {"status": "error", "error": msg, "finished_at": time.time()})
        db.update(j["kind"], j["target_id"], {"status": "error", "error": msg})
        n += 1
    return n


def friendly_error(e: Exception) -> str:
    text = str(e) or e.__class__.__name__
    return text[:600]


runner = JobRunner()
