"""Brand templates: watermark, logo, accent colour, caption preset, hook style, intro/outro cards."""
from __future__ import annotations
import time
from pathlib import Path
from . import db
from .config import DATA, cfg

LOGOS = DATA / "logos"

DEFAULT = {
    "watermark_text": "@TheTruthUntold",
    "logo": "",
    "accent": "#F5A524",
    "caption_preset": "auto",
    "hook_style": "box",
    "intro_card": False,
    "outro_card": False,
    "credit": True,
    "progress_bar": True,
    "outro_text": "Follow for more",
}


def ensure_default() -> dict:
    t = db.row("SELECT * FROM templates WHERE is_default=1")
    if t:
        return db.loads(t, "data")
    any_t = db.row("SELECT * FROM templates ORDER BY created_at LIMIT 1")
    if any_t:
        db.update("templates", any_t["id"], {"is_default": 1})
        return db.loads(any_t, "data")
    tid = db.new_id("t_")
    db.insert("templates", {"id": tid, "name": "TheTruthUntold", "is_default": 1, "data": dict(DEFAULT)})
    return db.loads(db.row("SELECT * FROM templates WHERE id=?", (tid,)), "data")


def get(tid: str | None) -> dict:
    t = db.loads(db.row("SELECT * FROM templates WHERE id=?", (tid,)), "data") if tid else None
    if not t:
        t = ensure_default()
    t["data"] = {**DEFAULT, **(t.get("data") or {})}
    return t


def all_templates() -> list[dict]:
    ensure_default()
    out = []
    for t in db.rows("SELECT * FROM templates ORDER BY is_default DESC, created_at"):
        db.loads(t, "data")
        t["data"] = {**DEFAULT, **(t["data"] or {})}
        out.append(t)
    return out


def save(tid: str | None, name: str, data: dict, make_default: bool = False) -> str:
    clean = {k: data.get(k, DEFAULT[k]) for k in DEFAULT}
    for k in ("intro_card", "outro_card", "credit", "progress_bar"):
        clean[k] = bool(clean[k]) and str(clean[k]).lower() not in ("false", "0", "off", "")
    clean["accent"] = _hex(clean["accent"]) or DEFAULT["accent"]
    if tid and db.row("SELECT id FROM templates WHERE id=?", (tid,)):
        db.update("templates", tid, {"name": name or "Template", "data": clean})
    else:
        tid = db.new_id("t_")
        db.insert("templates", {"id": tid, "name": name or "Template", "is_default": 0, "data": clean})
    if make_default:
        set_default(tid)
    return tid


def set_default(tid: str):
    db.execute("UPDATE templates SET is_default=0")
    db.update("templates", tid, {"is_default": 1})


def delete(tid: str):
    t = db.row("SELECT * FROM templates WHERE id=?", (tid,))
    if not t:
        return
    db.execute("DELETE FROM templates WHERE id=?", (tid,))
    if t["is_default"]:
        ensure_default()


def _hex(v: str) -> str | None:
    v = (v or "").strip()
    if v.startswith("#") and len(v) in (7, 9):
        try:
            int(v[1:], 16)
            return v.upper()
        except ValueError:
            return None
    return None


def save_logo(upload_bytes: bytes, ext: str) -> str:
    LOGOS.mkdir(parents=True, exist_ok=True)
    p = LOGOS / f"logo_{int(time.time())}{ext if ext in ('.png', '.jpg', '.jpeg', '.webp') else '.png'}"
    p.write_bytes(upload_bytes)
    return str(p)
