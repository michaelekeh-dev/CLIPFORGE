"""What the channel actually did. Real numbers from YouTube, and what they honestly say about when to post.

Two sources. The Data API (already allowed when you connected) gives every upload with its views, likes and the
exact minute it went live: enough to see which days and hours work for you. The Analytics API gives retention and
where views come from, and needs one extra tap to allow.

Everything here refuses to guess. A day or an hour is only called good when enough uploads back it up.
"""
from __future__ import annotations
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from . import db

SHORT_MAX_SECONDS = 180
# younger uploads are still collecting views, so they cannot be compared with older ones
SETTLE_DAYS = 7
# never call an hour "your best" off a single upload
MIN_PER_BUCKET = 3
MIN_VIDEOS = 12
CACHE_HOURS = 6
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DUR = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def seconds(iso: str) -> int:
    m = DUR.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = [int(x or 0) for x in m.groups()]
    return d * 86400 + h * 3600 + mi * 60 + s


def has_analytics() -> bool:
    """True when the channel was connected with retention allowed."""
    return ANALYTICS_SCOPE in ((db.get_setting("youtube_credentials") or {}).get("scopes") or [])


def _service(name: str, version: str):
    from googleapiclient.discovery import build
    from . import youtube
    creds = youtube.credentials()
    if not creds:
        raise RuntimeError("YouTube is not connected")
    return build(name, version, credentials=creds, cache_discovery=False)


# ----------------------------------------------------------------------------- the uploads
def fetch_videos(limit: int = 200) -> list[dict]:
    """Every recent upload with the numbers that matter: [{id, title, published, seconds, views, likes, comments, short}]."""
    yt = _service("youtube", "v3")
    ch = yt.channels().list(part="contentDetails", mine=True).execute().get("items") or []
    if not ch:
        return []
    playlist = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while len(ids) < limit:
        r = yt.playlistItems().list(part="contentDetails", playlistId=playlist, maxResults=50, pageToken=token).execute()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items") or []]
        token = r.get("nextPageToken")
        if not token:
            break
    out = []
    for i in range(0, len(ids[:limit]), 50):
        r = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for v in r.get("items") or []:
            st, sn = v.get("statistics") or {}, v.get("snippet") or {}
            secs = seconds((v.get("contentDetails") or {}).get("duration", ""))
            out.append({"id": v["id"], "title": sn.get("title", ""),
                        "published": _ts(sn.get("publishedAt", "")), "seconds": secs,
                        "views": int(st.get("viewCount") or 0), "likes": int(st.get("likeCount") or 0),
                        "comments": int(st.get("commentCount") or 0), "short": secs and secs <= SHORT_MAX_SECONDS})
    return sorted([v for v in out if v["published"]], key=lambda v: v["published"], reverse=True)


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def retention(video_ids: list[str]) -> dict:
    """{video_id: {kept_percent, seconds_watched, subs_gained}} from the Analytics API. Empty when not allowed yet."""
    if not has_analytics() or not video_ids:
        return {}
    ya = _service("youtubeAnalytics", "v2")
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=365)
    out = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        try:
            r = ya.reports().query(ids="channel==MINE", startDate=str(start), endDate=str(end),
                                   metrics="views,averageViewPercentage,averageViewDuration,subscribersGained",
                                   dimensions="video", filters="video==" + ",".join(chunk), maxResults=200).execute()
        except Exception as e:  # noqa: BLE001
            db.log_error("analytics", str(e))
            break
        for row in r.get("rows") or []:
            out[row[0]] = {"kept_percent": round(float(row[2] or 0), 1), "seconds_watched": round(float(row[3] or 0), 1),
                           "subs_gained": int(row[4] or 0)}
    return out


# ----------------------------------------------------------------------------- reading the numbers
def settled(videos: list[dict], shorts_only: bool = True) -> list[dict]:
    """Uploads old enough to judge fairly."""
    cutoff = time.time() - SETTLE_DAYS * 86400
    return [v for v in videos if v["published"] < cutoff and (v["short"] or not shorts_only)]


def best_times(videos: list[dict], z) -> dict:
    """Which day and which hour your uploads did best, or an honest 'not enough yet'."""
    rows = settled(videos)
    if len(rows) < MIN_VIDEOS:
        return {"enough": False, "have": len(rows), "need": MIN_VIDEOS, "days": [], "hours": [], "times": []}
    typical = statistics.median([v["views"] for v in rows]) or 1
    by_day, by_hour = {}, {}
    for v in rows:
        d = datetime.fromtimestamp(v["published"], z)
        by_day.setdefault(d.weekday(), []).append(v["views"])
        by_hour.setdefault(d.hour, []).append(v["views"])

    def rank(groups, label):
        out = []
        for key, vals in groups.items():
            if len(vals) < MIN_PER_BUCKET:
                continue
            med = statistics.median(vals)
            out.append({"key": key, "label": label(key), "uploads": len(vals), "median": int(med),
                        "times_normal": round(med / typical, 2)})
        return sorted(out, key=lambda r: -r["times_normal"])

    days = rank(by_day, lambda k: DAYS[k])
    hours = rank(by_hour, lambda k: f"{k:02d}:00")
    # only offer an hour as a posting slot when it is at least as good as a normal upload of yours
    good = [h for h in hours if h["times_normal"] >= 1.0][:2]
    return {"enough": True, "typical": int(typical), "days": days, "hours": hours,
            "times": [f"{h['key']:02d}:00" for h in good]}


def cadence(videos: list[dict]) -> dict:
    """How often you actually post, and whether posting more looked like it hurt."""
    shorts = [v for v in videos if v["short"]]
    recent = [v for v in shorts if v["published"] > time.time() - 30 * 86400]
    gaps = []
    for a, b in zip(shorts, shorts[1:]):
        g = (a["published"] - b["published"]) / 3600
        if 0 < g < 24 * 30:
            gaps.append(g)
    rows = settled(videos)
    per_day = {}
    for v in rows:
        day = datetime.fromtimestamp(v["published"], timezone.utc).date()
        per_day.setdefault(day, []).append(v["views"])
    one = [statistics.median(v) for v in per_day.values() if len(v) == 1]
    many = [statistics.median(v) for v in per_day.values() if len(v) > 1]
    cost = None
    if len(one) >= 3 and len(many) >= 3:
        cost = round(statistics.median(many) / (statistics.median(one) or 1), 2)
    return {"shorts_total": len(shorts), "last_30_days": len(recent),
            "per_week": round(len(recent) / 30 * 7, 1) if recent else 0.0,
            "median_gap_hours": round(statistics.median(gaps), 1) if gaps else None,
            "multi_post_effect": cost}


def summary(videos: list[dict], keep: dict) -> dict:
    shorts = [v for v in videos if v["short"]]
    rows = settled(videos)
    views = [v["views"] for v in rows]
    kept = [k["kept_percent"] for k in keep.values() if k.get("kept_percent")]
    best = max(rows, key=lambda v: v["views"]) if rows else None
    worst = min(rows, key=lambda v: v["views"]) if rows else None
    return {"shorts": len(shorts), "judged": len(rows),
            "median_views": int(statistics.median(views)) if views else 0,
            "best_views": max(views) if views else 0,
            "best_title": best["title"] if best else "", "best_id": best["id"] if best else "",
            "worst_title": worst["title"] if worst else "", "worst_views": worst["views"] if worst else 0,
            "median_kept": round(statistics.median(kept), 1) if kept else None,
            "subs_gained": sum(k.get("subs_gained", 0) for k in keep.values()) or None,
            "has_analytics": has_analytics()}


# ----------------------------------------------------------------------------- the advice
def advice(sm: dict, times: dict, cad: dict) -> list[str]:
    """Plain sentences. Each one only appears when the numbers actually support it."""
    out = []
    if sm["judged"] < MIN_VIDEOS:
        out.append(f"You have {sm['judged']} Shorts old enough to judge. At about {MIN_VIDEOS} this page stops guessing "
                   "and starts telling you which day and hour genuinely work for you. Until then, post daily and ignore timing.")
    if times.get("enough"):
        d = (times.get("days") or [None])[0]
        if d and d["times_normal"] >= 1.25:
            out.append(f"{d['label']} is your best day: {d['median']} views typical against {times['typical']} on a normal day "
                       f"({d['times_normal']}×, from {d['uploads']} uploads).")
        elif d:
            out.append("No day stands out. Your views are decided by the clip, not the calendar, so post whenever suits you.")
        h = (times.get("hours") or [None])[0]
        if h and h["times_normal"] >= 1.25:
            out.append(f"{h['label']} is your best hour ({h['times_normal']}× normal, {h['uploads']} uploads). "
                       "Shorts get pushed for days, so treat this as a nudge, not a rule.")
    if cad["last_30_days"] == 0:
        out.append("No Shorts in the last 30 days. Consistency matters more than any posting hour: get to one a day.")
    elif cad["per_week"] < 5:
        out.append(f"You are posting about {cad['per_week']} Shorts a week. Shorts reward volume: every extra clip is "
                   "another roll of the dice, and one good one lifts the whole channel. Aim for one a day.")
    elif cad["per_week"] > 21:
        out.append(f"You are posting about {cad['per_week']} Shorts a week. That is plenty. Spend the effort on picking "
                   "better moments instead of more of them.")
    if cad.get("multi_post_effect") is not None:
        c = cad["multi_post_effect"]
        if c < 0.75:
            out.append(f"On days you posted more than once, each clip did {int((1 - c) * 100)}% worse. Spread them out.")
        elif c > 1.1:
            out.append("Posting twice a day has not hurt you. Keep the two slots.")
    if sm.get("median_kept") is not None:
        k = sm["median_kept"]
        if k < 60:
            out.append(f"People watch {k}% of a typical clip. Under about 70% the opening is losing them: cut straight "
                       "to the hook and keep clips shorter.")
        elif k >= 85:
            out.append(f"People watch {k}% of a typical clip. That is strong: the picking is working, so post more.")
    elif not sm["has_analytics"]:
        out.append("Retention is the number that actually decides how far a Short travels, and it needs one extra tap to "
                   "allow. Reconnect the channel below to switch it on.")
    if sm.get("best_title"):
        out.append(f"Your best clip is \"{sm['best_title']}\" at {sm['best_views']} views. Make more like that one: "
                   "the title style that worked is the one to copy.")
    return out


def report(force: bool = False) -> dict:
    """The whole picture, cached for a few hours so we do not burn the YouTube quota."""
    from . import youtube, autopilot
    cached = db.get_setting("analytics_report") or {}
    if cached and not force and time.time() - cached.get("at", 0) < CACHE_HOURS * 3600:
        return cached
    if not youtube.connected():
        return {"ok": False, "detail": "Connect your YouTube channel first and this page fills itself in.", "at": time.time()}
    try:
        videos = fetch_videos()
    except Exception as e:  # noqa: BLE001
        db.log_error("analytics", str(e))
        return {"ok": False, "detail": f"YouTube would not hand over the numbers: {e}", "at": time.time()}
    if not videos:
        return {"ok": False, "detail": "This channel has no uploads yet.", "at": time.time()}
    keep = retention([v["id"] for v in settled(videos)][:100])
    z = autopilot.tz()
    times, cad = best_times(videos, z), cadence(videos)
    sm = summary(videos, keep)
    for v in videos:
        v["kept_percent"] = (keep.get(v["id"]) or {}).get("kept_percent")
    out = {"ok": True, "at": time.time(), "summary": sm, "times": times, "cadence": cad,
           "advice": advice(sm, times, cad), "top": sorted(settled(videos), key=lambda v: -v["views"])[:8],
           "recent": videos[:8], "timezone": str(z)}
    db.set_setting("analytics_report", out)
    return out


def text() -> str:
    """The same thing as a Telegram message."""
    r = report()
    if not r.get("ok"):
        return r.get("detail", "No numbers yet.")
    s, c = r["summary"], r["cadence"]
    lines = [f"📊 {s['shorts']} Shorts · {s['median_views']} views on a typical one · best {s['best_views']}",
             f"Posting about {c['per_week']} a week"]
    if s.get("median_kept") is not None:
        lines.append(f"People watch {s['median_kept']}% of a clip")
    if r["times"].get("enough") and r["times"]["days"]:
        d = r["times"]["days"][0]
        lines.append(f"Best day: {d['label']} ({d['times_normal']}× normal)")
    lines.append("")
    lines += ["• " + a for a in r["advice"][:4]]
    return "\n".join(lines)
