"""Autopilot: watch a channel, clip new episodes by themselves, send clips to Telegram, post to YouTube on a schedule."""
from __future__ import annotations
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import db, notify, youtube
from .config import cfg, env

DEFAULTS = {
    "enabled": False,
    "channel_url": "https://www.youtube.com/@JumpersJump",
    "check_minutes": 60,
    "clips": 5,
    "length": "auto",
    "keywords": list(cfg.get("moments.default_keywords", [])),
    "min_episode_minutes": 15,
    # ask = every clip waits for your Post tap in Telegram; auto = green clips are queued by themselves, yellow ask, red skip
    "mode": "ask",
    "post_times": ["11:00", "18:00"],
    "timezone": "Europe/London",
    "max_posts_per_day": 2,
    "description_footer": "Full episode: {source_url}\nCredit: {credit}\n\n#Shorts #TheTruthUntold",
    "public": True,
}


def get_settings() -> dict:
    return {**DEFAULTS, **(db.get_setting("autopilot") or {})}


def save_settings(changes: dict) -> dict:
    cur = get_settings()
    cur.update({k: v for k, v in changes.items() if k in DEFAULTS})
    db.set_setting("autopilot", cur)
    return cur


def tz():
    try:
        return ZoneInfo(get_settings().get("timezone") or "Europe/London")
    except Exception:
        return ZoneInfo("UTC")


def base_url() -> str:
    return (env("PUBLIC_URL") or db.get_setting("public_url") or "http://localhost:8000").rstrip("/")


# ----------------------------------------------------------------------------- watcher
def channel_feed(channel_url: str) -> list[dict]:
    """Latest videos of a channel: [{id, title, url, published}] via the RSS feed (needs the channel id, resolved once)."""
    import httpx
    import xml.etree.ElementTree as ET
    cid = db.get_setting("channel_id:" + channel_url)
    if not cid:
        cid = resolve_channel_id(channel_url)
        if cid:
            db.set_setting("channel_id:" + channel_url, cid)
    if not cid:
        return []
    r = httpx.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", timeout=30,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        vid = e.findtext("yt:videoId", "", ns)
        out.append({"id": vid, "title": e.findtext("a:title", "", ns), "url": f"https://www.youtube.com/watch?v={vid}",
                    "published": e.findtext("a:published", "", ns)})
    return out


def resolve_channel_id(channel_url: str) -> str | None:
    if "channel/" in channel_url:
        return channel_url.rstrip("/").split("channel/")[-1].split("/")[0]
    try:
        import yt_dlp
        from .download import _cookie_file
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True, "playlistend": 1}
        ck = _cookie_file()
        if ck:
            opts["cookiefile"] = ck
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url.rstrip("/") + "/videos", download=False)
        return info.get("channel_id") or info.get("uploader_id") or (info.get("id") if str(info.get("id", "")).startswith("UC") else None)
    except Exception as e:  # noqa: BLE001
        db.log_error("autopilot", f"channel id: {e}")
        return None


def start_project_from_url(url: str, title: str = "") -> str:
    from . import pipeline
    from .jobs import runner
    st = get_settings()
    opts = pipeline.default_options()
    opts.update({"clips": int(st["clips"]), "length": st["length"], "keywords": list(st["keywords"]), "autopilot": True})
    pid = pipeline.create_project(url, opts, title=title)
    runner.submit("projects", pid, lambda prog: pipeline.run_project(pid, prog))
    return pid


def check_channel_once() -> list[str]:
    """Look for new episodes; start a project for each unseen one. Returns started project ids."""
    st = get_settings()
    if not st.get("enabled") or not st.get("channel_url"):
        return []
    started = []
    try:
        feed = channel_feed(st["channel_url"])
    except Exception as e:  # noqa: BLE001
        db.log_error("autopilot", f"feed: {e}")
        return []
    first_run = not db.row("SELECT 1 FROM seen_videos LIMIT 1")
    for v in feed[:10]:
        if db.row("SELECT 1 FROM seen_videos WHERE video_id=?", (v["id"],)):
            continue
        if first_run and v is not feed[0]:
            # on the very first check only the newest episode is taken; older ones are just marked as seen
            db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": ""})
            continue
        if _looks_like_short(v):
            db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": ""})
            continue
        pid = start_project_from_url(v["url"], title=v["title"])
        db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": pid})
        started.append(pid)
        notify.send_text(f"🎬 New episode: <b>{notify._esc(v['title'])}</b>\nMaking clips now.")
    return started


def _looks_like_short(v: dict) -> bool:
    return bool(re.search(r"#shorts?\b", v.get("title", ""), re.I))


# ----------------------------------------------------------------------------- when a project finishes
def on_project_done(pid: str):
    """Send every clip to Telegram; queue green clips when mode is auto."""
    p = db.loads(db.row("SELECT * FROM projects WHERE id=?", (pid,)), "options", "info")
    if not p:
        return
    st = get_settings()
    method = (p.get("info") or {}).get("pick_method", "")
    if method.startswith("heuristic"):
        notify.send_text(f"⚠️ Claude did not pick these clips ({notify._esc(method)}). Check the Anthropic key and credit on the Status page.")
    clips = db.rows("SELECT * FROM clips WHERE project_id=? AND status='done' ORDER BY score DESC", (pid,))
    if not clips:
        notify.send_text(f"No clips came out of <b>{notify._esc(p['title'])}</b>. {base_url()}/project/{pid}")
        return
    notify.send_text(f"✂️ <b>{notify._esc(p['title'])}</b>: {len(clips)} clips ready.")
    for c in clips:
        d = db.loads(dict(c), "data")["data"] or {}
        verdict = (d.get("fact_check") or {}).get("verdict", "")
        if st["mode"] == "auto" and youtube.connected():
            if verdict == "ok":
                queue_post(c["id"])
            elif verdict == "skip":
                skip_clip(c["id"])
        notify.send_clip(c["id"], base_url())


# ----------------------------------------------------------------------------- posting queue
def next_slot(after: float | None = None) -> float:
    """Next free posting time from post_times, respecting max_posts_per_day."""
    st = get_settings()
    z = tz()
    now = datetime.fromtimestamp(after or time.time(), z)
    times = sorted(st.get("post_times") or ["12:00"])
    taken = {p["publish_at"] for p in db.rows("SELECT publish_at FROM posts WHERE status IN ('waiting','uploading','scheduled')") if p["publish_at"]}
    for day in range(0, 60):
        d = (now + timedelta(days=day)).date()
        used_today = sum(1 for t in taken if datetime.fromtimestamp(t, z).date() == d)
        if used_today >= int(st.get("max_posts_per_day", 2)):
            continue
        for hhmm in times:
            h, m = [int(x) for x in hhmm.split(":")]
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=z)
            ts = slot.timestamp()
            if ts <= time.time() + 120 or ts in taken:
                continue
            return ts
    return time.time() + 3600


def queue_post(clip_id: str) -> dict | None:
    c = db.row("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not c or c["status"] != "done":
        return None
    existing = db.row("SELECT * FROM posts WHERE clip_id=? AND status IN ('waiting','uploading','scheduled','uploaded','published')", (clip_id,))
    if existing:
        return existing
    pid_ = db.new_id("post_")
    db.insert("posts", {"id": pid_, "clip_id": clip_id, "project_id": c["project_id"], "status": "waiting", "publish_at": next_slot(),
                        "title": c["title"]})
    return db.row("SELECT * FROM posts WHERE id=?", (pid_,))


def skip_clip(clip_id: str):
    c = db.row("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not c:
        return
    for p in db.rows("SELECT * FROM posts WHERE clip_id=? AND status='waiting'", (clip_id,)):
        db.update("posts", p["id"], {"status": "skipped"})
    if not db.row("SELECT 1 FROM posts WHERE clip_id=?", (clip_id,)):
        db.insert("posts", {"id": db.new_id("post_"), "clip_id": clip_id, "project_id": c["project_id"], "status": "skipped", "title": c["title"]})
    db.execute("UPDATE clips SET data = json_set(data, '$.feedback', 'skip') WHERE id=?", (clip_id,))


def cancel_post(clip_id: str):
    for p in db.rows("SELECT * FROM posts WHERE clip_id=? AND status='waiting'", (clip_id,)):
        db.update("posts", p["id"], {"status": "cancelled"})


def post_text(clip: dict, project: dict) -> tuple[str, str, list[str]]:
    """Title, description and tags for YouTube from the clip's data and the description footer."""
    d = clip.get("data") or {}
    st = get_settings()
    title = (clip["title"] or d.get("title") or "Clip").strip()
    if "#shorts" not in title.lower() and len(title) <= 92:
        title = title + " #Shorts"
    hashtags = d.get("hashtags") or []
    credit = (project.get("options") or {}).get("credit_name") or project.get("channel") or ""
    footer = (st.get("description_footer") or "").format(source_url=project.get("source_url") or "", credit=credit)
    desc = (d.get("description") or "").strip() + "\n\n" + " ".join(hashtags) + "\n\n" + footer
    tags = [h.lstrip("#") for h in hashtags] + [k for k in (project.get("options") or {}).get("keywords", [])][:10]
    return title[:100], desc.strip(), tags


def run_due_posts() -> int:
    """Upload posts whose time has come (they are scheduled on YouTube a bit ahead of time)."""
    if not youtube.connected():
        return 0
    n = 0
    lead = 20 * 60  # upload 20 minutes before the slot; YouTube publishes it at the exact time
    for p in db.rows("SELECT * FROM posts WHERE status='waiting' AND publish_at <= ? ORDER BY publish_at", (time.time() + lead,)):
        clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", (p["clip_id"],)), "data", "settings")
        proj = db.loads(db.row("SELECT * FROM projects WHERE id=?", (p["project_id"],)), "options", "info")
        if not clip or not proj or not clip.get("path"):
            db.update("posts", p["id"], {"status": "error", "error": "clip missing"})
            continue
        db.update("posts", p["id"], {"status": "uploading"})
        try:
            title, desc, tags = post_text(clip, proj)
            st = get_settings()
            r = youtube.upload(clip["path"], title, desc, tags, publish_at=p["publish_at"], public=bool(st.get("public", True)))
            db.update("posts", p["id"], {"status": "scheduled" if r["status"] == "scheduled" else "uploaded", "youtube_id": r["id"], "title": title})
            notify.send_text(f"📤 Uploaded <b>{notify._esc(title)}</b> → https://youtu.be/{r['id']}" +
                             (f"\nGoes public {notify._fmt_time(p['publish_at'])}." if r["status"] == "scheduled" else
                              ("\nIt is private until YouTube verifies the API project (see DEPLOY.md), publish it from the YouTube app." if r["status"] == "private" else "")))
            n += 1
        except Exception as e:  # noqa: BLE001
            db.update("posts", p["id"], {"status": "error", "error": str(e)[:500]})
            db.log_error("youtube", str(e))
            notify.send_text(f"❌ Upload failed for <b>{notify._esc(clip['title'])}</b>: {notify._esc(str(e)[:200])}")
        notify.refresh_clip_message(p["clip_id"], base_url())
    return n


def status_text() -> str:
    st = get_settings()
    waiting = db.rows("SELECT * FROM posts WHERE status='waiting' ORDER BY publish_at")
    running = db.rows("SELECT title, stage FROM projects WHERE status IN ('running','queued')")
    lines = [f"Autopilot: {'on' if st['enabled'] else 'off'} · mode: {st['mode']} · channel: {st['channel_url']}",
             f"YouTube: {'connected' if youtube.connected() else 'not connected'}"]
    for r in running:
        lines.append(f"⏳ {r['title'][:50]} – {r['stage']}")
    for p in waiting:
        lines.append(f"🕒 {notify._fmt_time(p['publish_at'])} – {p['title'][:50]}")
    if not waiting:
        lines.append("Nothing waiting to post.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- threads
def _loop():
    last_check = 0.0
    while True:
        try:
            st = get_settings()
            if st.get("enabled") and time.time() - last_check > float(st.get("check_minutes", 60)) * 60:
                check_channel_once()
                last_check = time.time()
            run_due_posts()
        except Exception as e:  # noqa: BLE001
            db.log_error("autopilot", str(e))
        time.sleep(60)


def start_threads():
    threading.Thread(target=_loop, daemon=True, name="autopilot").start()
    notify.start_poller(base_url)
