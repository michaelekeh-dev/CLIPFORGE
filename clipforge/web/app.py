"""The web app. FastAPI + Jinja templates + a little vanilla JS."""
from __future__ import annotations
import json
import secrets
import time
from pathlib import Path
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from .. import db, pipeline, factcheck, captions
from ..config import cfg, env, PROJECTS, UPLOADS, ROOT, CACHE, device
from ..jobs import runner
from .. import __version__

HERE = Path(__file__).parent
app = FastAPI(title="CLIPFORGE", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
db.init_db()

SESSION_DAYS = 30


def _secret() -> str:
    s = env("SECRET_KEY")
    if s:
        return s
    r = db.row("SELECT value FROM settings WHERE key='secret_key'")
    if r:
        return r["value"]
    s = secrets.token_urlsafe(32)
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('secret_key', ?)", (s,))
    return s


signer = URLSafeTimedSerializer(_secret(), salt="clipforge-session")


def logged_in(request: Request) -> bool:
    if not env("APP_PASSWORD"):
        return True
    tok = request.cookies.get("cf_session")
    if not tok:
        return False
    try:
        signer.loads(tok, max_age=SESSION_DAYS * 86400)
        return True
    except BadSignature:
        return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/health", "/manifest.webmanifest", "/sw.js") or logged_in(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "Please log in"}, status_code=401)
    return RedirectResponse(f"/login?next={path}", status_code=303)


def page(request: Request, name: str, **ctx):
    ctx.update({"request": request, "version": __version__, "llm_live": bool(env("ANTHROPIC_API_KEY")), "device": device(),
                "has_password": bool(env("APP_PASSWORD"))})
    return templates.TemplateResponse(request, name, ctx)


# ----------------------------------------------------------------------------- auth
_attempts: dict[str, list[float]] = {}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if logged_in(request):
        return RedirectResponse(next or "/", status_code=303)
    return page(request, "login.html", error="", next=next)


@app.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    now = time.time()
    tries = [t for t in _attempts.get(ip, []) if now - t < 600]
    if len(tries) >= 10:
        return page(request, "login.html", error="Too many tries. Wait 10 minutes.", next=next)
    if secrets.compare_digest(password, env("APP_PASSWORD")):
        resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
        resp.set_cookie("cf_session", signer.dumps({"t": now}), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=bool(cfg.get("app.https", False)))
        _attempts.pop(ip, None)
        return resp
    tries.append(now)
    _attempts[ip] = tries
    return page(request, "login.html", error="Wrong password", next=next)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("cf_session")
    return resp


# ----------------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page(request, "home.html", projects=list_projects(), defaults=pipeline.default_options(),
                lengths=cfg.get("moments.lengths", {}), presets=caption_presets())


@app.get("/project/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str):
    p = project_json(pid)
    if not p:
        raise HTTPException(404)
    return page(request, "project.html", project=p, presets=caption_presets())


def caption_presets() -> list[dict]:
    out = []
    pdir = CACHE / "previews"
    pdir.mkdir(parents=True, exist_ok=True)
    for p in captions.preset_names():
        png = pdir / f"{p['id']}.png"
        if not png.exists():
            try:
                captions.render_preview(p["id"], png)
            except Exception as e:  # noqa: BLE001
                db.log_error("preview", str(e))
        out.append({**p, "preview_url": f"/previews/{p['id']}.png" if png.exists() else ""})
    return out


@app.get("/previews/{name}.png")
def preview_png(name: str):
    f = CACHE / "previews" / f"{name}.png"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/api/captions/presets")
def api_presets():
    return {"presets": caption_presets()}


@app.post("/api/clips/{cid}/settings")
async def api_clip_settings(cid: str, request: Request):
    c = db.loads(db.row("SELECT * FROM clips WHERE id=?", (cid,)), "settings")
    if not c:
        raise HTTPException(404)
    body = await request.json()
    allowed = {"style", "emoji", "ratio", "layout", "captions", "hook", "hook_text", "zooms", "filler", "broll", "template", "progress_bar"}
    new = {**(c["settings"] or {}), **{k: v for k, v in body.items() if k in allowed}}
    db.update("clips", cid, {"settings": new})
    if body.get("render"):
        runner.submit("clips", cid, lambda prog: pipeline.render_one(cid, prog))
    return {"ok": True, "settings": new}


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "jobs_running": runner.running_count()}


# ----------------------------------------------------------------------------- api
def list_projects() -> list[dict]:
    out = []
    for p in db.rows("SELECT * FROM projects ORDER BY created_at DESC"):
        db.loads(p, "options", "info")
        c = db.row("SELECT COUNT(*) AS n, SUM(status='done') AS done FROM clips WHERE project_id=?", (p["id"],))
        p["clip_count"] = c["n"] if c else 0
        p["clips_done"] = c["done"] or 0 if c else 0
        p["thumb_url"] = f"/media/{p['id']}/thumb.jpg" if p.get("thumbnail") else ""
        out.append(_public_project(p))
    return out


def _public_project(p: dict) -> dict:
    keep = ("id", "title", "source_url", "source_type", "channel", "duration", "status", "stage", "progress", "error",
            "options", "created_at", "updated_at", "clip_count", "clips_done", "thumb_url", "info")
    return {k: p.get(k) for k in keep}


def project_json(pid: str) -> dict | None:
    p = db.loads(db.row("SELECT * FROM projects WHERE id=?", (pid,)), "options", "info")
    if not p:
        return None
    p["thumb_url"] = f"/media/{pid}/thumb.jpg" if p.get("thumbnail") else ""
    clips = []
    for c in db.rows("SELECT * FROM clips WHERE project_id=? ORDER BY score DESC, idx", (pid,)):
        clips.append(clip_json(c))
    out = _public_project(p)
    out["clips"] = clips
    return out


def clip_json(c: dict) -> dict:
    db.loads(c, "data", "settings")
    d = c["data"] or {}
    fc = d.get("fact_check") or {}
    fname = Path(c["path"]).name if c.get("path") else ""
    return {
        "id": c["id"], "project_id": c["project_id"], "idx": c["idx"], "start": c["start"], "end": c["end"],
        "duration": round((c["end"] or 0) - (c["start"] or 0), 2), "score": c["score"], "title": c["title"],
        "status": c["status"], "stage": c["stage"], "progress": c["progress"], "error": c["error"],
        "video_url": f"/media/{c['project_id']}/clips/{fname}" if fname and c["status"] == "done" else "",
        "thumb_url": f"/media/{c['project_id']}/clips/{Path(c['thumbnail']).name}" if c.get("thumbnail") else "",
        "download_url": f"/api/clips/{c['id']}/download",
        "reasons": d.get("reasons") or {}, "topic": d.get("topic", ""), "description": d.get("description", ""),
        "hashtags": d.get("hashtags") or [], "original_title": d.get("title", ""), "text": d.get("text", ""),
        "fact_check": fc, "badge": factcheck.badge(fc.get("verdict", "")), "settings": c["settings"] or {},
        "pick_method": d.get("pick_method", ""),
    }


@app.get("/api/projects")
def api_projects():
    return {"projects": list_projects(), "jobs_running": runner.running_count()}


@app.get("/api/projects/{pid}")
def api_project(pid: str):
    p = project_json(pid)
    if not p:
        raise HTTPException(404)
    return p


def _parse_time(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    parts = [float(x) for x in s.split(":")]
    t = 0.0
    for x in parts:
        t = t * 60 + x
    return t


@app.post("/api/projects")
async def api_create_project(request: Request, url: str = Form(""), clips: int = Form(5), length: str = Form("auto"),
                             keywords: str = Form(""), start: str = Form(""), end: str = Form(""),
                             style: str = Form("auto"), emoji: str = Form("on"), layout: str = Form("auto"),
                             file: UploadFile | None = File(None)):
    opts = pipeline.default_options()
    opts.update({"clips": max(1, min(20, int(clips))), "length": length if length in ("auto", "short", "medium", "long") else "auto",
                 "keywords": [k.strip() for k in keywords.split(",") if k.strip()], "start": _parse_time(start), "end": _parse_time(end),
                 "style": style, "emoji": emoji in ("on", "true", "1"),
                 "layout": layout if layout in ("auto", "single", "split", "wide") else "auto"})
    title = ""
    if file is not None and file.filename:
        UPLOADS.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename).suffix.lower() or ".mp4"
        dest = UPLOADS / (db.new_id("up_") + ext)
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        source = str(dest)
        title = Path(file.filename).stem
    elif url.strip():
        source = url.strip()
    else:
        raise HTTPException(400, "Paste a link or choose a file")
    pid = pipeline.create_project(source, opts, title=title)
    runner.submit("projects", pid, lambda prog: pipeline.run_project(pid, prog))
    return {"id": pid}


@app.post("/api/projects/{pid}/retry")
def api_retry(pid: str):
    p = db.row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    runner.submit("projects", pid, lambda prog: pipeline.run_project(pid, prog))
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def api_delete(pid: str):
    pipeline.delete_project(pid)
    return {"ok": True}


@app.post("/api/clips/{cid}/render")
def api_render_clip(cid: str):
    c = db.row("SELECT * FROM clips WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    runner.submit("clips", cid, lambda prog: pipeline.render_one(cid, prog))
    return {"ok": True}


@app.get("/api/clips/{cid}/download")
def api_download(cid: str):
    c = db.row("SELECT * FROM clips WHERE id=?", (cid,))
    if not c or not c["path"] or not Path(c["path"]).exists():
        raise HTTPException(404, "Clip not rendered yet")
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in (c["title"] or "clip"))[:60].strip() or "clip"
    return FileResponse(c["path"], media_type="video/mp4", filename=f"{c['idx']:02d} {safe}.mp4")


@app.get("/media/{pid}/{path:path}")
def media_file(pid: str, path: str):
    base = (PROJECTS / pid).resolve()
    f = (base / path).resolve()
    if not str(f).startswith(str(base)) or not f.exists() or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)
