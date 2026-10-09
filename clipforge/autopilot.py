"""Autopilot: watch a channel, clip new episodes by themselves, send clips to Telegram, post to YouTube on a schedule."""
from __future__ import annotations
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import db, notify, youtube, review, llm, download
from .config import cfg, env

DEFAULTS = {
    "enabled": False,
    "channel_url": "https://www.youtube.com/@JumpersJump",
    "check_minutes": 60,
    "clips": 15,
    "length": "auto",
    "keywords": list(cfg.get("moments.default_keywords", [])),
    "min_episode_minutes": 15,
    # ask = every clip waits for your Post tap in Telegram; auto = green clips are queued by themselves, yellow ask, red skip
    "mode": "ask",
    "post_times": ["11:00", "18:00"],
    "timezone": "Europe/London",
    "max_posts_per_day": 2,
    # keep posting from the channel's older episodes whenever the queue runs low, so a quiet week
    # on the source channel does not mean a quiet week on yours
    "backfill": True,
    # start another episode when fewer than this many days of posts are queued up
    "queue_days": 3,
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
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def _read_feed(cid: str) -> tuple[int, list[dict]]:
    """(status code, videos) for one channel id. A wrong id answers 404 with no entries."""
    import httpx
    import xml.etree.ElementTree as ET
    r = httpx.get(FEED_URL.format(cid), timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        return r.status_code, []
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        vid = e.findtext("yt:videoId", "", ns)
        out.append({"id": vid, "title": e.findtext("a:title", "", ns), "url": f"https://www.youtube.com/watch?v={vid}",
                    "published": e.findtext("a:published", "", ns)})
    return 200, out


def verified_channel_id(channel_url: str) -> tuple[str | None, list[dict], str]:
    """The channel id whose feed actually answers, with that feed. (id, videos, what went wrong).

    Scraping a page for an id is a guess: a consent page or a bit of boilerplate can hand back a
    perfectly well-formed id belonging to somebody else, which then 404s forever. So every candidate
    is tried against the real feed and only one that answers is kept."""
    tried = []
    for cid in channel_candidates(channel_url):
        if cid in tried:
            continue
        tried.append(cid)
        try:
            code, vids = _read_feed(cid)
        except Exception as e:  # noqa: BLE001
            db.log_error("autopilot", f"feed {cid}: {str(e)[:150]}")
            continue
        if code == 200 and vids:
            return cid, vids, ""
        db.log_error("autopilot", f"channel id {cid} does not answer (HTTP {code}) — trying the next candidate")
    if not tried:
        return None, [], ("No channel id could be read from that link. Open the channel on YouTube and copy the "
                          "address bar; a link with /channel/UC... in it always works.")
    return None, [], ("Tried " + ", ".join(tried) + " and YouTube knows none of them. Copy the channel's link from "
                      "the address bar on its page — a /channel/UC... link is the one that cannot be guessed wrong.")


def channel_feed(channel_url: str) -> list[dict]:
    """Latest videos of a channel. The id is resolved once, but only ever cached once it has answered."""
    cid = db.get_setting("channel_id:" + channel_url)
    if cid and valid_channel_id(cid):
        try:
            code, vids = _read_feed(cid)
            if code == 200 and vids:
                return vids
            db.log_error("autopilot", f"cached channel id {cid} stopped answering (HTTP {code}) — resolving again")
        except Exception as e:  # noqa: BLE001
            db.log_error("autopilot", f"feed {cid}: {str(e)[:150]}")
            return []
    elif cid:
        db.log_error("autopilot", f"cached channel id {cid!r} is malformed ({len(cid)} characters, not 24) — resolving again")
    db.execute("DELETE FROM settings WHERE key=?", ("channel_id:" + channel_url,))
    got, vids, why = verified_channel_id(channel_url)
    if not got:
        if why:
            db.log_error("autopilot", "channel: " + why)
        return []
    db.set_setting("channel_id:" + channel_url, got)
    return vids


CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def valid_channel_id(cid: str) -> bool:
    """A YouTube channel id is UC plus exactly 22 characters. Anything else 404s the feed forever."""
    return bool(CHANNEL_ID.match((cid or "").strip()))


def channel_candidates(channel_url: str) -> list[str]:
    """Every id this link might mean, best guess first. The caller proves which one is real."""
    url = (channel_url or "").strip()
    out = []

    def add(c):
        c = str(c or "").strip()
        if valid_channel_id(c) and c not in out:
            out.append(c)

    if not url:
        return out
    add(url)
    if "channel/" in url:
        add(url.rstrip("/").split("channel/")[-1].split("/")[0])
    # the channel page carries its own id — but so does every other channel linked from it, so collect
    # them all in page order and let the feed decide which one is this channel's
    try:
        import httpx
        import re as _re
        from .download import proxy_url
        px = proxy_url()
        r = httpx.get(url.rstrip("/"), timeout=30, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
                      **({"proxy": px} if px else {}))
        for pat in (r'"(?:channelId|externalId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                    r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                    r'channel/(UC[A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])'):
            for m in _re.finditer(pat, r.text):
                add(m.group(1))
    except Exception as e:  # noqa: BLE001
        db.log_error("autopilot", f"channel page: {str(e)[:200]}")
    try:
        import yt_dlp
        from .download import _cookie_file
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True, "playlistend": 1}
        ck = _cookie_file()
        if ck:
            opts["cookiefile"] = ck
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.rstrip("/") + "/videos", download=False)
        for cand in (info.get("channel_id"), info.get("uploader_id"), info.get("id")):
            add(cand)
    except Exception as e:  # noqa: BLE001
        db.log_error("autopilot", f"channel id: {str(e)[:200]}")
    return out


def resolve_channel_id(channel_url: str) -> str | None:
    """The id behind a channel link, proven against the feed before it is believed."""
    cid, _, _ = verified_channel_id(channel_url)
    return cid


def channel_check(channel_url: str = "") -> dict:
    """Say out loud whether the watcher can actually see this channel, and what it would clip next."""
    url = (channel_url or get_settings().get("channel_url") or "").strip()
    if not url:
        return {"ok": False, "detail": "No channel set yet.", "videos": []}
    cid = db.get_setting("channel_id:" + url) or resolve_channel_id(url)
    if not cid:
        return {"ok": False, "url": url, "videos": [],
                "detail": "Could not work out the channel id from that link. Open the channel on YouTube, copy the "
                          "link from the address bar, and paste that. A link with /channel/UC... in it always works."}
    db.set_setting("channel_id:" + url, cid)
    try:
        feed = channel_feed(url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "channel_id": cid, "videos": [],
                "detail": f"Found the channel ({cid}) but its video list would not load: {str(e)[:160]}"}
    if not feed:
        return {"ok": False, "url": url, "channel_id": cid, "videos": [],
                "detail": f"Found the channel ({cid}) but it lists no videos."}
    fresh = [v for v in feed if not db.row("SELECT 1 FROM seen_videos WHERE video_id=?", (v["id"],))
             and not _looks_like_short(v)]
    return {"ok": True, "url": url, "channel_id": cid, "videos": feed[:8], "new": len(fresh),
            "detail": f"Watching {len(feed)} recent videos. {len(fresh)} not clipped yet."}


def start_project_from_url(url: str, title: str = "") -> str:
    from . import pipeline
    from .jobs import runner
    from .config import cfg as _c
    st = get_settings()
    if bool(_c.get("storage.one_episode_at_a_time", True)):
        # a new episode means the last one is done with: clear it out before downloading gigabytes
        try:
            pipeline.make_room(reason=title[:40] or url)
        except Exception as e:  # noqa: BLE001
            db.log_error("storage", str(e))
    opts = pipeline.default_options()
    opts.update({"clips": int(st["clips"]), "length": st["length"], "keywords": list(st["keywords"]), "autopilot": True})
    pid = pipeline.create_project(url, opts, title=title)
    runner.submit("projects", pid, "project")
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
    db.set_setting("last_channel_check", time.time())
    first_run = not db.row("SELECT 1 FROM seen_videos LIMIT 1")
    newest_episode = None  # on the first look only one episode is clipped — and it must be an episode,
    for v in feed[:10]:    # not whichever Short happened to be posted most recently
        if db.row("SELECT 1 FROM seen_videos WHERE video_id=?", (v["id"],)):
            continue
        if _looks_like_short(v):
            db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": "short"})
            continue
        why = episode_problem(v["url"], v["title"])
        if why:
            # remembered as handled, so it is never looked at again
            db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": "short"})
            db.log_error("autopilot", f"skipped {v['title'][:60]}: {why}")
            continue
        if newest_episode is None:
            newest_episode = v["id"]
        elif first_run:
            # the rest are the back catalogue: remembered now, clipped later when the queue runs low
            db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": ""})
            continue
        pid = start_project_from_url(v["url"], title=v["title"])
        db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(), "project_id": pid})
        started.append(pid)
        notify.send_text(f"🎬 New episode: <b>{notify._esc(v['title'])}</b>\nMaking clips now.")
    return started


def _looks_like_short(v: dict) -> bool:
    """What we can tell from the feed alone: the hashtag, or a /shorts/ link."""
    if re.search(r"#shorts?\b", v.get("title", ""), re.I):
        return True
    return "/shorts/" in (v.get("url") or "")


# YouTube caps a Short at three minutes; the slack is for a long upload that is still one
MAX_SHORT_SECONDS = 240


def episode_problem(url: str, title: str = "") -> str:
    """Why this video is not an episode, or '' when it is one.

    The feed lists a channel's Shorts next to its episodes, and a Short is already a finished vertical
    video. Clipping one just burns an hour of CPU to put our subtitles over someone else's edit."""
    st = get_settings()
    info = download.peek(url)
    secs = float((info or {}).get("duration") or 0)
    mins = secs / 60
    low = float(st.get("min_episode_minutes", 15) or 0)
    if not info or not secs:
        # YouTube would not say how long it is. That is NOT a pass: it is downloaded and measured for
        # real before anything is clipped (pipeline.run_project), so a Short cannot slip through here.
        return ""
    # A Short is vertical AND brief. Tall on its own is not enough: an episode filmed or posted
    # vertically is still an episode, and judging on shape alone threw a real one away.
    if info.get("vertical") and secs <= MAX_SHORT_SECONDS:
        return (f"it is a {secs:.0f} second vertical video ({info.get('width')}x{info.get('height')}) — "
                "that is a Short, not an episode")
    if low and mins < low:
        return f"it is only {mins:.1f} minutes long, and an episode is at least {low:.0f}"
    return ""


def too_short_to_clip(seconds: float) -> str:
    """The one rule that decides it, used on the real file once it is downloaded."""
    low = float(get_settings().get("min_episode_minutes", 15) or 0)
    mins = float(seconds or 0) / 60
    if not low:
        return ""
    if not seconds:
        return "its length could not be read at all"
    if mins < low:
        return f"it is {mins:.1f} minutes long and an episode is at least {low:.0f}"
    return ""


def reject_as_short(pid: str, why: str):
    """Stop an autopilot project that turned out to be a Short, and remember never to take it again."""
    from . import pipeline
    proj = db.row("SELECT * FROM projects WHERE id=?", (pid,)) or {}
    vid = db.row("SELECT video_id FROM seen_videos WHERE project_id=?", (pid,))
    if vid:
        db.execute("UPDATE seen_videos SET project_id='short' WHERE video_id=?", (vid["video_id"],))
    db.update("projects", pid, {"status": "skipped", "stage": "Not an episode", "error": why, "progress": 100})
    try:
        pipeline.drop_source(pid)
    except Exception as e:  # noqa: BLE001
        db.log_error("storage", str(e))
    db.log_error("autopilot", f"not an episode: {(proj.get('title') or pid)[:60]} — {why}")
    notify.send_text(f"⏭ Skipped <b>{notify._esc((proj.get('title') or pid)[:60])}</b>: {notify._esc(why)}. "
                     "Only long-form episodes get clipped.")


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
    held = 0
    for c in clips:
        d = db.loads(dict(c), "data")["data"] or {}
        verdict = (d.get("fact_check") or {}).get("verdict", "")
        rev = d.get("review") or {}
        if st["mode"] == "auto" and youtube.connected():
            if review.blocks_autopost(rev):
                held += 1  # the clip is cut badly or the framing is wrong: it waits for you
            elif verdict == "ok":
                queue_post(c["id"])
            elif verdict == "skip":
                skip_clip(c["id"])
        notify.send_clip(c["id"], base_url())
    if held:
        notify.send_text(f"🔎 {held} clip{'s' if held > 1 else ''} held back for you to look at: cut mid-sentence or "
                         "the framing is off. They are above with the reason on each one.")


# ----------------------------------------------------------------------------- posting queue
def days_queued() -> float:
    """How many days of posting are already lined up."""
    n = (db.row("SELECT COUNT(*) AS n FROM posts WHERE status IN ('waiting','uploading')") or {}).get("n", 0)
    per_day = max(1, int(get_settings().get("max_posts_per_day", 2) or 2))
    return round(n / per_day, 2)


def backlog() -> list[dict]:
    """Episodes from the channel we know about but have never clipped, newest first."""
    return db.rows("SELECT * FROM seen_videos WHERE (project_id IS NULL OR project_id='') ORDER BY rowid")


def backfill_once(force: bool = False) -> str | None:
    """When the queue runs low, clip one more of the channel's older episodes. One at a time.

    `force` is you asking for one now (Telegram /older), so the queue-depth check is skipped."""
    st = get_settings()
    if not force and not (st.get("enabled") and st.get("backfill")):
        return None
    if not force and days_queued() >= float(st.get("queue_days", 3) or 3):
        return None
    if db.row("SELECT 1 FROM projects WHERE status IN ('running','queued') LIMIT 1"):
        return None  # something is already being clipped; do not pile up downloads
    pool = backlog()
    if not pool:
        # nothing recorded to fall back on: ask the channel for its older videos and use one we have
        # never clipped. This is what makes a quiet week fill itself in from the back catalogue.
        try:
            for v in channel_feed(st.get("channel_url") or ""):
                if db.row("SELECT 1 FROM seen_videos WHERE video_id=?", (v["id"],)):
                    continue
                db.insert("seen_videos", {"video_id": v["id"], "title": v["title"], "seen_at": time.time(),
                                          "project_id": "short" if _looks_like_short(v) else ""})
            pool = backlog()
        except Exception as e:  # noqa: BLE001
            db.log_error("autopilot", f"backfill feed: {str(e)[:200]}")
    nxt = None
    for v in pool:
        v = {**v, "url": v.get("url") or f"https://www.youtube.com/watch?v={v['video_id']}"}
        if _looks_like_short(v):
            db.execute("UPDATE seen_videos SET project_id='short' WHERE video_id=?", (v["video_id"],))
            continue
        why = episode_problem(f"https://www.youtube.com/watch?v={v['video_id']}", v.get("title") or "")
        if why:
            db.execute("UPDATE seen_videos SET project_id='short' WHERE video_id=?", (v["video_id"],))
            db.log_error("autopilot", f"skipped {(v.get('title') or '')[:60]}: {why}")
            continue
        nxt = v
        break
    if not nxt:
        return None
    url = f"https://www.youtube.com/watch?v={nxt['video_id']}"
    pid = start_project_from_url(url, title=nxt.get("title") or "")
    db.execute("UPDATE seen_videos SET project_id=? WHERE video_id=?", (pid, nxt["video_id"]))
    notify.send_text(f"📼 Queue was getting low, so I'm clipping an older episode: "
                     f"<b>{notify._esc(nxt.get('title') or nxt['video_id'])}</b>")
    return pid


def waiting_clips() -> list[dict]:
    """Finished clips you have never tapped Post or Skip on."""
    return db.rows("SELECT * FROM clips WHERE status='done' AND id NOT IN (SELECT clip_id FROM posts) "
                   "ORDER BY created_at")


def purge_short_sourced_clips() -> tuple[int, int]:
    """Throw out clips whose source video was never an episode. Returns (removed, kept).

    Clips made before the length rule existed keep arriving in Telegram no matter what the rule says
    now, because they are already rendered. This clears them out by the one fact that settles it:
    how long the video they were cut from actually was."""
    from . import pipeline
    low = float(get_settings().get("min_episode_minutes", 15) or 0)
    removed = kept = 0
    for c in db.rows("SELECT c.id AS id, c.path AS path, p.duration AS dur FROM clips c "
                     "JOIN projects p ON p.id = c.project_id WHERE c.status='done'"):
        secs = float(c["dur"] or 0)
        if secs and secs / 60 >= low:
            kept += 1
            continue
        if not secs:
            kept += 1       # unknown source length: leave it rather than throw away a real episode
            continue
        skip_clip(c["id"])
        pipeline._unlink_ours(c.get("path") or "")
        removed += 1
    return removed, kept


def clear_waiting_clips() -> int:
    """Skip every clip still waiting for a decision, and free the disk they were holding."""
    from . import pipeline
    n = 0
    for c in waiting_clips():
        skip_clip(c["id"])
        pipeline._unlink_ours(c.get("path") or "")
        n += 1
    return n


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
            # never in the past, and never at or before the time we were asked to start looking from
            if ts <= max(time.time() + 120, after or 0) or ts in taken:
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
    title = (d.get("post_title") or clip["title"] or d.get("title") or "Clip").strip()
    if "#shorts" not in title.lower() and len(title) <= 92:
        title = title + " #Shorts"
    hashtags = d.get("hashtags") or []
    credit = (project.get("options") or {}).get("credit_name") or project.get("channel") or ""
    footer = (st.get("description_footer") or "").format(source_url=project.get("source_url") or "", credit=credit)
    if d.get("post_description"):
        # you wrote this one yourself: keep it word for word, only add the credit footer if it is missing
        body = d["post_description"].strip()
        desc = body if footer.strip() and footer.strip() in body else body + "\n\n" + footer
    else:
        desc = (d.get("description") or "").strip() + "\n\n" + " ".join(hashtags) + "\n\n" + footer
    tags = [h.lstrip("#") for h in hashtags] + [k for k in (project.get("options") or {}).get("keywords", [])][:10]
    return title[:100], desc.strip(), tags


def upload_post(p: dict) -> bool:
    """Send one queued post to YouTube. Publishes straight away when its time is now."""
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", (p["clip_id"],)), "data", "settings")
    proj = db.loads(db.row("SELECT * FROM projects WHERE id=?", (p["project_id"],)), "options", "info")
    if not clip or not proj or not clip.get("path"):
        db.update("posts", p["id"], {"status": "error", "error": "clip missing"})
        return False
    db.update("posts", p["id"], {"status": "uploading"})
    notify.refresh_clip_message(p["clip_id"], base_url())
    try:
        title, desc, tags = post_text(clip, proj)
        st = get_settings()
        r = youtube.upload(clip["path"], title, desc, tags, publish_at=p["publish_at"], public=bool(st.get("public", True)))
        db.update("posts", p["id"], {"status": "scheduled" if r["status"] == "scheduled" else "uploaded", "youtube_id": r["id"], "title": title})
        if bool(cfg.get("storage.one_episode_at_a_time", True)):
            # it is on YouTube now; re-render from the editor if you ever need the file back
            from . import pipeline as _pl
            _pl._unlink_ours(clip["path"])
        notify.send_text(f"📤 Uploaded <b>{notify._esc(title)}</b> → https://youtu.be/{r['id']}" +
                         (f"\nGoes public {notify._fmt_time(p['publish_at'])}." if r["status"] == "scheduled" else
                          ("\nIt is private until YouTube verifies the API project (see DEPLOY.md), publish it from the YouTube app." if r["status"] == "private" else "")))
        ok = True
    except Exception as e:  # noqa: BLE001
        db.update("posts", p["id"], {"status": "error", "error": str(e)[:500]})
        db.log_error("youtube", str(e))
        notify.send_text(f"❌ Upload failed for <b>{notify._esc(clip['title'])}</b>: {notify._esc(str(e)[:200])}")
        ok = False
    notify.refresh_clip_message(p["clip_id"], base_url())
    return ok


def run_due_posts() -> int:
    """Upload posts whose time has come (they are scheduled on YouTube a bit ahead of time)."""
    if not youtube.connected():
        return 0
    n = 0
    lead = 20 * 60  # upload 20 minutes before the slot; YouTube publishes it at the exact time
    for p in db.rows("SELECT * FROM posts WHERE status='waiting' AND publish_at <= ? ORDER BY publish_at", (time.time() + lead,)):
        if upload_post(p):
            n += 1
    return n


def post_now(clip_id: str) -> dict | None:
    """Put this clip on YouTube right now, jumping the queue. Uploads in the background."""
    p = queue_post(clip_id)
    if not p:
        return None
    if p["status"] in ("uploading", "uploaded", "scheduled", "published"):
        return None  # already on YouTube: never post the same clip twice
    db.update("posts", p["id"], {"publish_at": time.time(), "status": "waiting"})
    p = db.row("SELECT * FROM posts WHERE id=?", (p["id"],))
    threading.Thread(target=upload_post, args=(p,), daemon=True, name="post-now").start()
    return p


def reschedule(clip_id: str, when: float) -> dict | None:
    """Move a clip's post to a time you picked."""
    p = queue_post(clip_id)
    if not p or p["status"] not in ("waiting", "error"):
        return None
    db.update("posts", p["id"], {"publish_at": when, "status": "waiting", "error": ""})
    return db.row("SELECT * FROM posts WHERE id=?", (p["id"],))


def slot_choices(count: int = 3) -> list[float]:
    """The next few free posting slots, for the Schedule buttons."""
    out, after = [], None
    for _ in range(count):
        t = next_slot(after)
        if out and t <= out[-1]:
            break
        out.append(t)
        after = t + 60
    return out


def set_post_text(clip_id: str, field: str, value: str):
    """Remember a title or description you typed yourself."""
    key = "post_title" if field == "title" else "post_description"
    db.execute("UPDATE clips SET data = json_set(data, ?, ?) WHERE id=?", ("$." + key, value.strip(), clip_id))
    if field == "title":
        db.execute("UPDATE posts SET title=? WHERE clip_id=? AND status IN ('waiting','error')", (value.strip()[:100], clip_id))


def ready() -> dict:
    """Is the hands-off chain actually complete? Each answer is a thing you can go and fix."""
    st = get_settings()
    steps = [("Autopilot switched on", bool(st.get("enabled")), "turn it on at the top of this page"),
             ("A channel to watch", bool(st.get("channel_url")), "paste the channel link"),
             ("YouTube connected", youtube.connected(), "connect your channel"),
             ("Telegram linked", bool(notify.enabled() and notify.chat_id()), "send /start to your bot"),
             ("Posts without asking", st.get("mode") == "auto", 'set Posting to "Post by itself"'),
             ("Claude picking the clips", llm.mode() == "live", "add a workspace Anthropic key")]
    missing = [(name, how) for name, ok, how in steps if not ok]
    return {"steps": [{"name": n, "ok": o, "how": h} for n, o, h in steps], "missing": missing,
            "ok": not missing, "days_queued": days_queued(), "backlog": len(backlog()),
            "times": ", ".join(sorted(st.get("post_times") or [])), "per_day": st.get("max_posts_per_day")}


def status_text() -> str:
    st = get_settings()
    waiting = db.rows("SELECT * FROM posts WHERE status='waiting' ORDER BY publish_at")
    running = db.rows("SELECT title, stage FROM projects WHERE status IN ('running','queued')")
    r = ready()
    lines = [f"Autopilot: {'on' if st['enabled'] else 'off'} · mode: {st['mode']} · channel: {st['channel_url']}",
             f"YouTube: {'connected' if youtube.connected() else 'not connected'}",
             (f"✅ Fully hands off: posting {r['per_day']}× a day at {r['times']}." if r["ok"]
              else "⚠️ Not hands off yet — " + "; ".join(f"{n} ({h})" for n, h in r["missing"]))]
    for r in running:
        lines.append(f"⏳ {r['title'][:50]} – {r['stage']}")
    for p in waiting:
        lines.append(f"🕒 {notify._fmt_time(p['publish_at'])} – {p['title'][:50]}")
    if not waiting:
        lines.append("Nothing waiting to post.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- threads
def last_activity() -> dict:
    """When each part of the chain last did something."""
    def when(sql, args=()):
        r = db.row(sql, args)
        return float((r or {}).get("t") or 0)
    return {
        "channel_check": float(db.get_setting("last_channel_check") or 0),
        "clip_made": when("SELECT MAX(created_at) AS t FROM clips WHERE status='done'"),
        "posted": when("SELECT MAX(updated_at) AS t FROM posts WHERE status IN ('uploaded','scheduled','published')"),
        "project_started": when("SELECT MAX(created_at) AS t FROM projects"),
    }


def why_quiet() -> str:
    """One honest paragraph on why nothing is arriving, or '' when the chain is working."""
    st = get_settings()
    if not st.get("enabled"):
        return "Autopilot is switched off, so nothing is being clipped. Turn it on on the Autopilot page."
    r = ready()
    if r["missing"]:
        return ("Autopilot cannot run on its own yet — " +
                "; ".join(f"{n} ({h})" for n, h in r["missing"]) + ".")
    act = last_activity()
    now = time.time()
    queued = days_queued()
    waiting_clips = (db.row("SELECT COUNT(*) AS n FROM clips WHERE status='done' AND id NOT IN "
                            "(SELECT clip_id FROM posts)") or {}).get("n", 0)
    stale_check = act["channel_check"] and now - act["channel_check"] > 3 * 3600
    never_checked = not act["channel_check"]
    bits = []
    if never_checked:
        bits.append("the channel has never been checked, so the watcher is not running — the app may not have "
                    "restarted since this was set up")
    elif stale_check:
        bits.append(f"the channel was last checked {(now - act['channel_check']) / 3600:.0f} hours ago, which is "
                    f"longer than the {st.get('check_minutes')} minute setting — the watcher looks stuck")
    if queued > 0:
        bits.append(f"{queued} days of posts are already lined up, so no new episode is being clipped on purpose")
    elif not backlog():
        bits.append("nothing is queued and there are no older episodes left to fall back on, so there is nothing "
                    "to post until the channel puts out something new")
    if waiting_clips:
        bits.append(f"{waiting_clips} clips are sitting in Telegram waiting for you to tap Post or Skip")
    err = db.row("SELECT message, at FROM errors WHERE where_ LIKE 'autopilot%' OR where_ LIKE 'projects%' "
                 "ORDER BY id DESC LIMIT 1")
    if err and now - float(err["at"] or 0) < 48 * 3600:
        bits.append("the last thing that went wrong was: " + str(err["message"]).split("\n")[0][:200])
    if not bits:
        return ""
    return "Nothing has arrived because " + "; and ".join(bits) + "."


def heartbeat() -> str:
    """Once a day, if nothing is arriving, say why instead of going silent. Returns what was sent."""
    if not bool(cfg.get("app.heartbeat", True)):
        return ""
    last = float(db.get_setting("last_heartbeat") or 0)
    if time.time() - last < 24 * 3600:
        return ""
    act = last_activity()
    quiet_for = time.time() - max(act["clip_made"], act["posted"])
    if quiet_for < 24 * 3600:
        db.set_setting("last_heartbeat", time.time())  # it is working; reset the clock and stay quiet
        return ""
    why = why_quiet()
    db.set_setting("last_heartbeat", time.time())
    if not why:
        return ""
    notify.send_text("🤔 <b>Nothing posted in the last day.</b>\n" + notify._esc(why) +
                     f"\n\n{base_url()}/autopilot")
    return why


def _loop():
    while True:
        try:
            st = get_settings()
            last_check = float(db.get_setting("last_channel_check") or 0)
            if st.get("enabled") and time.time() - last_check > float(st.get("check_minutes", 60)) * 60:
                check_channel_once()
                # written down, not kept in a variable: otherwise nobody can tell whether it ever ran
                db.set_setting("last_channel_check", time.time())
            backfill_once()
            run_due_posts()
            heartbeat()
        except Exception as e:  # noqa: BLE001
            db.log_error("autopilot", str(e))
        time.sleep(60)


# Checking less often than once a day is never what anyone meant: the watcher exists to notice a new
# episode, and a week-long gap makes it useless. 10000 minutes (a typo for 1000, or just a slip) is the
# value that caused this.
MAX_CHECK_MINUTES = 1440


def apply_once(name: str, changes: dict) -> bool:
    """Make a settings change a single time, ever. Recorded, so a later choice of yours is not overruled."""
    done = list(db.get_setting("autopilot_migrations") or [])
    if name in done:
        return False
    save_settings(changes)
    db.set_setting("autopilot_migrations", sorted(set(done) | {name}))
    return True


def fix_impossible_settings() -> list[str]:
    """Repair settings that cannot do what they were set for. Runs once on startup."""
    st = get_settings()
    fixed = []
    # the old rule refused any vertical video, so real episodes posted tall were thrown away for good.
    # hand every one of those verdicts back to be judged again by the rule that also reads the length.
    if apply_once("reopen_shorts_verdicts", {}):
        db.execute("UPDATE seen_videos SET project_id='' WHERE project_id='short'")
        again = (db.row("SELECT COUNT(*) AS n FROM seen_videos WHERE project_id=''") or {}).get("n", 0)
        fixed.append(f"re-opened videos skipped as Shorts by the old shape-only rule ({again} now up for a "
                     "second look)")
    # the form used to cap this at 10, so the saved value is a limit of the old UI, not a choice.
    # raised once to 15, never lowered, and never touched again after that.
    want = max(15, int(st.get("clips", 0) or 0))
    if int(st.get("clips", 0) or 0) < 15 and apply_once("clips_at_least_15", {"clips": want}):
        fixed.append(f"clips per episode {st.get('clips')} -> {want} (the old form would not allow more than 10)")
    if float(st.get("check_minutes") or 60) > MAX_CHECK_MINUTES:
        was = st["check_minutes"]
        save_settings({"check_minutes": 60})
        fixed.append(f"check_minutes {was} -> 60 (it was checking the channel every {float(was) / 1440:.1f} days)")
    for note in fixed:
        db.log_error("autopilot", "setting repaired: " + note)
    return fixed


def start_threads():
    try:
        fix_impossible_settings()
    except Exception as e:  # noqa: BLE001
        db.log_error("autopilot", str(e))
    threading.Thread(target=_loop, daemon=True, name="autopilot").start()
    notify.start_poller(base_url)
