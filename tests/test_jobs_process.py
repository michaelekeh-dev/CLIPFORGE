"""Heavy work runs in its own process, so its memory goes back to the host when it finishes."""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, jobs  # noqa: E402


def _wait(check, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.2)
    return False


def test_the_worker_refuses_a_task_it_does_not_know():
    assert jobs.runner  # the module imports cleanly
    from clipforge import worker
    assert worker.main(["nonsense", "clips", "c1", "job1"]) == 2
    assert set(worker.TASKS) == {"project", "clip"}


def test_a_real_job_runs_in_a_child_process_and_reports_back(monkeypatch):
    """The job is for a clip that does not exist, so it must fail — but it has to fail in the child
    and come back as a recorded error, which is what proves the whole path works."""
    monkeypatch.delenv("CLIPFORGE_INLINE_JOBS", raising=False)
    db.init_db()
    db.execute("DELETE FROM clips WHERE id='c_proc_missing'")
    db.insert("clips", {"id": "c_proc_missing", "project_id": "p_nope", "status": "pending",
                        "start": 0, "end": 10, "title": "ghost", "data": "{}", "settings": "{}"})
    job_id = jobs.runner.submit("clips", "c_proc_missing", "clip")
    assert _wait(lambda: (db.row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status")
                 in ("done", "error"))
    job = db.row("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "error" and job["error"]
    assert int(job["owner_pid"]) != os.getpid(), "the work must not have run in the web process"
    db.execute("DELETE FROM clips WHERE id='c_proc_missing'")


def test_a_child_killed_outright_is_reported_as_out_of_memory(monkeypatch):
    """An out-of-memory kill leaves no error behind, so the job would sit at 'running' forever."""
    monkeypatch.delenv("CLIPFORGE_INLINE_JOBS", raising=False)
    db.init_db()
    db.execute("DELETE FROM clips WHERE id='c_proc_oom'")
    db.insert("clips", {"id": "c_proc_oom", "project_id": "p_nope", "status": "running",
                        "start": 0, "end": 10, "title": "ghost", "data": "{}", "settings": "{}"})
    job_id = db.new_id("job_")
    db.insert("jobs", {"id": job_id, "kind": "clips", "target_id": "c_proc_oom", "status": "running"})

    class Killed:
        pid = 999999
        returncode = -9

        def communicate(self):
            return ("", None)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Killed())
    jobs.runner._run_child(job_id, "clips", "c_proc_oom", "clip")
    job = db.row("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "error"
    assert "memory" in job["error"].lower()
    assert db.row("SELECT status FROM clips WHERE id='c_proc_oom'")["status"] == "error"
    db.execute("DELETE FROM clips WHERE id='c_proc_oom'")


def test_a_child_that_already_recorded_its_error_is_not_overwritten(monkeypatch):
    db.init_db()
    db.execute("DELETE FROM clips WHERE id='c_proc_own'")
    db.insert("clips", {"id": "c_proc_own", "project_id": "p_nope", "status": "running",
                        "start": 0, "end": 10, "title": "ghost", "data": "{}", "settings": "{}"})
    job_id = db.new_id("job_")
    db.insert("jobs", {"id": job_id, "kind": "clips", "target_id": "c_proc_own", "status": "running"})

    class Failed:
        pid = 999998
        returncode = 1

        def communicate(self_inner):
            db.update("jobs", job_id, {"status": "error", "error": "No speech was found in this video."})
            return ("", None)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Failed())
    jobs.runner._run_child(job_id, "clips", "c_proc_own", "clip")
    assert db.row("SELECT error FROM jobs WHERE id=?", (job_id,))["error"] == "No speech was found in this video."
    db.execute("DELETE FROM clips WHERE id='c_proc_own'")


def test_inline_mode_is_opt_in(monkeypatch):
    monkeypatch.delenv("CLIPFORGE_INLINE_JOBS", raising=False)
    assert jobs.inline() is False
    monkeypatch.setenv("CLIPFORGE_INLINE_JOBS", "1")
    assert jobs.inline() is True
    monkeypatch.setenv("CLIPFORGE_INLINE_JOBS", "0")
    assert jobs.inline() is False
