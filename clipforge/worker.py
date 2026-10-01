"""Run one heavy job in its own process.

Rendering loads a 780 MB speech model plus OpenCV and MediaPipe buffers. Python hands almost none of
that back, so when the job ran inside the web server the container sat on gigabytes of RAM forever —
and a host that bills per GB-minute charges for every one of those idle minutes. A process that exits
gives all of it back to the operating system.

    python -m clipforge.worker <task> <kind> <target_id> <job_id>
"""
from __future__ import annotations
import sys
import time
import traceback

TASKS = {"project": ("pipeline", "run_project"), "clip": ("pipeline", "render_one")}


def run(task: str, kind: str, target_id: str, job_id: str) -> int:
    from . import db
    from .jobs import Progress, friendly_error
    db.init_db()
    prog = Progress(job_id, kind, target_id)
    mod, name = TASKS[task]
    fn = getattr(__import__(f"clipforge.{mod}", fromlist=[mod]), name)
    try:
        fn(target_id, prog)
    except Exception as e:  # noqa: BLE001
        msg = friendly_error(e)
        db.log_error(f"{kind}:{target_id}", msg + "\n" + traceback.format_exc())
        db.update("jobs", job_id, {"status": "error", "error": msg, "finished_at": time.time()})
        db.update(kind, target_id, {"status": "error", "error": msg})
        return 1
    db.update("jobs", job_id, {"status": "done", "progress": 100, "finished_at": time.time()})
    db.update(kind, target_id, {"status": "done", "progress": 100})
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 4 or argv[0] not in TASKS:
        print(f"usage: python -m clipforge.worker [{'|'.join(TASKS)}] <kind> <target_id> <job_id>", file=sys.stderr)
        return 2
    return run(*argv)


if __name__ == "__main__":
    raise SystemExit(main())
