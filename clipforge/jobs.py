"""Background jobs with progress.

The heavy work runs in a separate process, not in the web server. Loading the speech model costs
780 MB that Python never gives back, so an in-process job left the container holding it for the rest
of the month. A child process hands all of it back the moment it exits, which is most of the hosting
bill on anything that charges per GB-minute of RAM.

Set CLIPFORGE_INLINE_JOBS=1 to run jobs in a thread instead (used by the tests).
"""
from __future__ import annotations
import os
import subprocess
import sys
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
        self.children: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, target_id: str, task: str) -> str:
        """Queue one job. `task` is 'project' or 'clip' — see clipforge.worker."""
        job_id = db.new_id("job_")
        db.insert("jobs", {"id": job_id, "kind": kind, "target_id": target_id, "status": "queued", "owner_pid": os.getpid()})
        db.update(kind, target_id, {"status": "queued", "stage": "Waiting to start", "progress": 0, "error": ""})
        self.pool.submit(self._run, job_id, kind, target_id, task)
        return job_id

    def _run(self, job_id, kind, target_id, task):
        db.update("jobs", job_id, {"status": "running", "started_at": time.time()})
        db.update(kind, target_id, {"status": "running"})
        with self._lock:
            self.running[job_id] = threading.current_thread()
        try:
            if inline():
                from .worker import run
                run(task, kind, target_id, job_id)
            else:
                self._run_child(job_id, kind, target_id, task)
        except Exception as e:  # noqa: BLE001
            msg = friendly_error(e)
            db.log_error(f"{kind}:{target_id}", msg + "\n" + traceback.format_exc())
            db.update("jobs", job_id, {"status": "error", "error": msg, "finished_at": time.time()})
            db.update(kind, target_id, {"status": "error", "error": msg})
        finally:
            with self._lock:
                self.running.pop(job_id, None)

    def _run_child(self, job_id, kind, target_id, task):
        cmd = [sys.executable, "-m", "clipforge.worker", task, kind, target_id, job_id]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # the child owns the job now, so a restart check looks at the right process
        db.update("jobs", job_id, {"owner_pid": proc.pid})
        with self._lock:
            self.children[job_id] = proc
        try:
            out, _ = proc.communicate()
        finally:
            with self._lock:
                self.children.pop(job_id, None)
        if proc.returncode == 0:
            return
        job = db.row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}
        if job.get("status") in ("done", "error"):
            return  # the child already recorded what went wrong
        # killed rather than failed: out of memory is by far the likeliest reason
        msg = ("The job stopped before it could finish. This usually means the server ran out of memory — "
               "try fewer clips per episode, or give the container more RAM.")
        db.log_error(f"{kind}:{target_id}", f"{msg} (exit {proc.returncode})\n{(out or '')[-2000:]}")
        db.update("jobs", job_id, {"status": "error", "error": msg, "finished_at": time.time()})
        db.update(kind, target_id, {"status": "error", "error": msg})

    def running_count(self) -> int:
        with self._lock:
            return len(self.running)


def inline() -> bool:
    """Run jobs in a thread instead of a child process. Only the tests want this."""
    return os.environ.get("CLIPFORGE_INLINE_JOBS", "") not in ("", "0", "false")


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
