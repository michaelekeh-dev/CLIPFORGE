"""CLI:  python -m clipforge <url or file> [--clips 5] [--length auto] [--ratio 9:16] [--out output]
        python -m clipforge serve [--port 8000]"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        return serve(argv[1:])
    ap = argparse.ArgumentParser(prog="clipforge", description="Turn a long talking video into vertical Shorts.")
    ap.add_argument("source", help="YouTube link or a local video file")
    ap.add_argument("--clips", type=int, default=None, help="how many clips (default 5)")
    ap.add_argument("--length", default="auto", choices=["auto", "short", "medium", "long"])
    ap.add_argument("--keywords", default=None, help="comma separated topic keywords")
    ap.add_argument("--start", type=float, default=None, help="only use the video from this second")
    ap.add_argument("--end", type=float, default=None, help="only use the video up to this second")
    ap.add_argument("--layout", default="auto", help="auto | single | split | wide")
    ap.add_argument("--style", default="auto", help="caption preset, e.g. bold_pop, clean, faith")
    ap.add_argument("--ratio", default="9:16", choices=["9:16", "1:1", "16:9"])
    ap.add_argument("--no-emoji", action="store_true", help="no emoji in captions")
    ap.add_argument("--out", default="output", help="output folder (default ./output)")
    args = ap.parse_args(argv)

    from . import db, pipeline
    from .config import cfg
    db.init_db()
    opts = pipeline.default_options()
    if args.clips:
        opts["clips"] = args.clips
    opts.update({"length": args.length, "start": args.start, "end": args.end, "layout": args.layout,
                 "style": args.style, "ratio": args.ratio, "emoji": not args.no_emoji})
    if args.keywords:
        opts["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]
    src = args.source
    if not src.startswith("http"):
        src = str(Path(src).resolve())
    pid = pipeline.create_project(src, opts)
    print(f"project {pid}")
    t0 = time.time()

    def progress(stage, pct=None, status=None):
        db.update("projects", pid, {"stage": stage, "progress": pct or 0})
        print(f"  [{(pct or 0):5.1f}%] {stage}")

    db.update("projects", pid, {"status": "running"})
    try:
        pipeline.run_project(pid, progress)
        db.update("projects", pid, {"status": "done", "progress": 100})
    except Exception as e:
        db.update("projects", pid, {"status": "error", "error": str(e)})
        print("FAILED:", e)
        return 1
    out_dir = Path(args.out) / pid
    out_dir.mkdir(parents=True, exist_ok=True)
    from .config import PROJECTS
    for f in sorted((PROJECTS / pid / "clips").glob("clip_*")):
        shutil.copy(f, out_dir / f.name)
    clips = db.rows("SELECT idx, score, title, start, \"end\", status FROM clips WHERE project_id=? ORDER BY idx", (pid,))
    print(f"\nDone in {time.time() - t0:.0f}s. {len(clips)} clips in {out_dir}/")
    for c in clips:
        print(f"  clip_{c['idx']:02d}.mp4  score {int(c['score']):3d}  {c['start']:7.1f}-{c['end']:7.1f}  {c['status']:6s}  {c['title']}")
    return 0


def serve(argv):
    ap = argparse.ArgumentParser(prog="clipforge serve")
    from .config import cfg
    ap.add_argument("--host", default=cfg.get("app.host", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(cfg.get("app.port", 8000)))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)
    import uvicorn
    uvicorn.run("clipforge.web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    sys.exit(main())
