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
from .. import db, pipeline, factcheck, captions, brand
from ..config import cfg, env, PROJECTS, UPLOADS, ROOT, CACHE, device
from ..jobs import runner
from .. import __version__

HERE = Path(__file__).parent
app = FastAPI(title="CLIPFORGE", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
db.init_db()
from .editor import router as editor_router  # noqa: E402
app.include_router(editor_router)

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
                lengths=cfg.get("moments.lengths", {}), presets=caption_presets(), templates_=brand.all_templates())


@app.get("/project/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str):
    p = project_json(pid)
    if not p:
        raise HTTPException(404)
    return page(request, "project.html", project=p, presets=caption_presets(), templates_=brand.all_templates())


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
    allowed = {"style", "emoji", "ratio", "layout", "captions", "hook", "hook_text", "zooms", "filler", "broll", "template", "progress_bar",
               "credit", "intro_card", "outro_card"}
    new = {**(c["settings"] or {}), **{k: v for k, v in body.items() if k in allowed}}
    db.update("clips", cid, {"settings": new})
    if body.get("render"):
        runner.submit("clips", cid, lambda prog: pipeline.render_one(cid, prog))
    return {"ok": True, "settings": new}


# ----------------------------------------------------------------------------- brand templates
@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    return page(request, "templates.html", templates_=brand.all_templates(), presets=caption_presets())


@app.get("/api/templates")
def api_templates():
    return {"templates": [{"id": t["id"], "name": t["name"], "is_default": bool(t["is_default"]), "data": t["data"]} for t in brand.all_templates()]}


@app.post("/api/templates")
async def api_save_template(request: Request, id: str = Form(""), name: str = Form("Template"), watermark_text: str = Form(""),
                            accent: str = Form("#F5A524"), caption_preset: str = Form("auto"), hook_style: str = Form("box"),
                            intro_card: str = Form(""), outro_card: str = Form(""), credit: str = Form(""), progress_bar: str = Form(""),
                            outro_text: str = Form("Follow for more"), make_default: str = Form(""), keep_logo: str = Form("1"),
                            logo: UploadFile | None = File(None)):
    existing = brand.get(id) if id else None
    logo_path = existing["data"].get("logo", "") if (existing and existing["id"] == id and keep_logo == "1") else ""
    if logo is not None and logo.filename:
        logo_path = brand.save_logo(await logo.read(), Path(logo.filename).suffix.lower())
    data = {"watermark_text": watermark_text.strip(), "logo": logo_path, "accent": accent, "caption_preset": caption_preset,
            "hook_style": hook_style if hook_style in ("box", "bar", "plain") else "box", "intro_card": intro_card == "on",
            "outro_card": outro_card == "on", "credit": credit == "on", "progress_bar": progress_bar == "on", "outro_text": outro_text.strip()}
    tid = brand.save(id or None, name.strip(), data, make_default=(make_default == "on"))
    return RedirectResponse("/templates", status_code=303)


@app.post("/api/templates/{tid}/default")
def api_template_default(tid: str):
    brand.set_default(tid)
    return {"ok": True}


@app.delete("/api/templates/{tid}")
def api_template_delete(tid: str):
    brand.delete(tid)
    return {"ok": True}


@app.get("/logos/{name}")
def logo_file(name: str):
    f = (brand.LOGOS / name).resolve()
    if not str(f).startswith(str(brand.LOGOS.resolve())) or not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


@app.post("/api/projects/{pid}/settings")
async def api_project_settings(pid: str, request: Request):
    p = db.loads(db.row("SELECT * FROM projects WHERE id=?", (pid,)), "options")
    if not p:
        raise HTTPException(404)
    body = await request.json()
    opts = dict(p["options"] or {})
    if "credit_name" in body:
        opts["credit_name"] = (body.get("credit_name") or "").strip()
    if "title" in body and body["title"].strip():
        db.update("projects", pid, {"title": body["title"].strip()[:200]})
    db.update("projects", pid, {"options": opts})
    return {"ok": True, "options": opts}


@app.get("/api/projects/{pid}/download.zip")
def api_download_zip(pid: str):
    import zipfile
    p = db.row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    clips = db.rows("SELECT * FROM clips WHERE project_id=? AND status='done' ORDER BY idx", (pid,))
    if not clips:
        raise HTTPException(404, "No finished clips yet")
    zpath = PROJECTS / pid / "clips.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        lines = []
        for c in clips:
            if c["path"] and Path(c["path"]).exists():
                safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in (c["title"] or "clip"))[:50].strip() or "clip"
                z.write(c["path"], f"{c['idx']:02d} {safe}.mp4")
                big = Path(c["path"]).with_name(Path(c["path"]).stem + "_thumb.jpg")
                if big.exists():
                    z.write(big, f"{c['idx']:02d} {safe} thumbnail.jpg")
                d = db.loads(dict(c), "data")["data"] or {}
                lines.append(f"{c['idx']:02d}. {c['title']}\n\n{d.get('description', '')}\n\n{' '.join(d.get('hashtags') or [])}\n\n---\n")
        z.writestr("titles and descriptions.txt", "\n".join(lines))
    safe_t = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in (p["title"] or "clips"))[:60].strip() or "clips"
    return FileResponse(zpath, media_type="application/zip", filename=f"{safe_t} - clips.zip")


@app.get("/api/clips/{cid}/thumbnail")
def api_clip_thumbnail(cid: str):
    c = db.row("SELECT * FROM clips WHERE id=?", (cid,))
    if not c or not c["path"]:
        raise HTTPException(404)
    big = Path(c["path"]).with_name(Path(c["path"]).stem + "_thumb.jpg")
    f = big if big.exists() else Path(c["thumbnail"] or "")
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg", filename=f"{c['idx']:02d} thumbnail.jpg")


@app.get("/clip/{cid}", response_class=HTMLResponse)
def clip_editor_page(request: Request, cid: str):
    c = db.row("SELECT * FROM clips WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    return page(request, "editor.html", clip=clip_json(dict(c)), presets=caption_presets(), templates_=brand.all_templates())


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
        "hook": (d.get("render") or {}).get("hook") or {"text": d.get("hook", "")},
        "filler": (d.get("render") or {}).get("filler") or {},
        "zooms": (d.get("render") or {}).get("zooms") or [],
        "thumbnail_url": f"/api/clips/{c['id']}/thumbnail" if c.get("path") else "",
        "broll": (d.get("render") or {}).get("broll") or {},
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
                             filler: str = Form("light"), hook: str = Form("on"), zooms: str = Form("on"), progress_bar: str = Form("on"),
                             ratio: str = Form("9:16"), template: str = Form(""), broll: str = Form("on"),
                             file: UploadFile | None = File(None)):
    opts = pipeline.default_options()
    opts.update({"clips": max(1, min(20, int(clips))), "length": length if length in ("auto", "short", "medium", "long") else "auto",
                 "keywords": [k.strip() for k in keywords.split(",") if k.strip()], "start": _parse_time(start), "end": _parse_time(end),
                 "style": style, "emoji": emoji in ("on", "true", "1"),
                 "layout": layout if layout in ("auto", "single", "split", "wide") else "auto",
                 "filler": filler if filler in ("off", "light", "aggressive") else "light",
                 "hook": hook in ("on", "true", "1"), "zooms": zooms in ("on", "true", "1"), "progress_bar": progress_bar in ("on", "true", "1"),
                 "ratio": ratio if ratio in ("9:16", "1:1", "16:9") else "9:16", "template": template, "broll": broll in ("on", "true", "1")})
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
