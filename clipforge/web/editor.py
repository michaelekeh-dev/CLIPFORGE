"""Clip editor API."""
from __future__ import annotations
import json
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from .. import db, pipeline, edits as ed
from ..config import PROJECTS
from ..jobs import runner

router = APIRouter()
WINDOW = 45.0  # seconds of transcript shown before and after the clip


def _clip(cid: str) -> dict:
    c = db.loads(db.row("SELECT * FROM clips WHERE id=?", (cid,)), "data", "settings")
    if not c:
        raise HTTPException(404)
    return c


@router.get("/api/clips/{cid}/editor")
def editor_data(cid: str):
    from .app import clip_json
    c = _clip(cid)
    p = db.loads(db.row("SELECT * FROM projects WHERE id=?", (c["project_id"],)), "options", "info")
    tr_path = PROJECTS / c["project_id"] / "transcript.json"
    if not tr_path.exists():
        raise HTTPException(409, "Transcript not ready")
    tr = json.loads(tr_path.read_text())
    e = ed.normalise((c["settings"] or {}).get("edits"), c["start"], c["end"])
    lo, hi = max(0.0, min(e["start"], c["start"]) - WINDOW), max(e["end"], c["end"]) + WINDOW
    words = [{"i": i, **w} for i, w in enumerate(tr["words"]) if lo <= w["s"] <= hi]
    render = (c["data"] or {}).get("render") or {}
    shots = (render.get("reframe") or {}).get("shots") or []
    out = clip_json(dict(c))
    out.update({"edits": e, "words": words, "orig_start": c["start"], "orig_end": c["end"], "timeline": (c["data"] or {}).get("timeline") or [[c["start"], c["end"]]],
                "shots": shots, "project_title": p["title"], "video_duration": p["duration"], "lead_in": render.get("lead_in", 0),
                "render": {k: render.get(k) for k in ("duration", "filler", "zooms", "hook", "captions", "broll") if k in render}})
    return out


@router.post("/api/clips/{cid}/edits")
async def save_edits(cid: str, request: Request):
    c = _clip(cid)
    body = await request.json()
    settings = dict(c["settings"] or {})
    if "edits" in body:
        settings["edits"] = ed.normalise(body["edits"], c["start"], c["end"])
    allowed = {"style", "emoji", "ratio", "layout", "captions", "hook", "hook_text", "zooms", "filler", "broll", "template", "progress_bar",
               "credit", "intro_card", "outro_card", "broll_items"}
    for k, v in (body.get("settings") or {}).items():
        if k in allowed:
            settings[k] = v
    db.update("clips", cid, {"settings": settings})
    if body.get("render"):
        runner.submit("clips", cid, lambda prog: pipeline.render_one(cid, prog))
    return {"ok": True, "settings": settings}


@router.post("/api/clips/{cid}/reset")
def reset_edits(cid: str):
    c = _clip(cid)
    settings = dict(c["settings"] or {})
    settings.pop("edits", None)
    db.update("clips", cid, {"settings": settings})
    return {"ok": True}
