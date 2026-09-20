"""Get the source video: yt-dlp for links, plain copy for uploads. Cached by video id."""
from __future__ import annotations
import json
import os
import re
import shutil
import time
from pathlib import Path
from . import media, db
from .config import CACHE, cfg, env

DL_DIR = CACHE / "downloads"


class DownloadBlocked(Exception):
    """YouTube (or the site) refused the download. The app offers an upload instead."""


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip(), re.I))


# messages that mean "YouTube refused us", as opposed to an ordinary failure
BLOCK_HINTS = ("sign in to confirm", "page needs to be reloaded", "not a bot", "403", "forbidden", "login required",
               "private video", "this video is unavailable", "blocked", "captcha", "429", "too many requests",
               "unable to extract")
# a format problem is not a block: the client let us in but offered nothing matching the rule
FORMAT_HINTS = ("requested format is not available", "no video formats found", "no formats found")


def _cookie_file() -> str | None:
    # YTDLP_COOKIES_B64: the cookies.txt file base64-encoded on one line (easiest on hosts like Railway)
    b64 = env("YTDLP_COOKIES_B64")
    if b64:
        import base64
        try:
            text = base64.b64decode("".join(b64.split())).decode("utf-8", "ignore")
            if "\t" in text or "# Netscape" in text:
                target = CACHE / "cookies.txt"
                target.write_text(text)
                return str(target)
        except Exception:
            pass
    c = env("YTDLP_COOKIES")
    if not c:
        return None
    p = Path(c)
    if p.exists():
        return str(p)
    # Allow pasting cookie text directly into the env var
    if "\t" in c or "# Netscape" in c:
        target = CACHE / "cookies.txt"
        target.write_text(c)
        return str(target)
    return None


def download(url: str, progress=None) -> dict:
    """Returns dict(path, id, title, channel, duration, thumbnail_url, webpage_url)."""
    import yt_dlp

    max_h = int(cfg.get("download.max_height", 1080))
    fmt = (f"bv*[height<={max_h}][ext=mp4]+ba[ext=m4a]/bv*[height<={max_h}]+ba/"
           f"b[height<={max_h}][ext=mp4]/b[height<={max_h}]/bv*+ba/b/best")

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                progress("Downloading", 2 + 18 * done / total)

    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(DL_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": int(cfg.get("download.retries", 3)),
        "fragment_retries": 5,
        "progress_hooks": [hook],
        "writeinfojson": False,
        "socket_timeout": 60,
    }
    ck = _cookie_file()
    if ck:
        opts["cookiefile"] = ck
    # yt-dlp can solve YouTube's JS challenges when a JS runtime is present
    for rt in ("deno", "node"):
        if shutil.which(rt):
            opts["js_runtimes"] = {rt: {}}
            break
    # proof-of-origin tokens from the bgutil helper (POT_PROVIDER_URL=http://host:4416) let server IPs through
    xargs = {}
    pot = env("POT_PROVIDER_URL")
    if pot:
        xargs["youtubepot-bgutilhttp"] = {"base_url": [pot.rstrip("/")]}
    if xargs:
        opts["extractor_args"] = dict(xargs)

    # Cached?
    info = None
    probe_err = None
    for client in (None, ["tv"], ["mweb"]):
        po = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, **({"cookiefile": ck} if ck else {})}
        if xargs or client:
            po["extractor_args"] = {**xargs, **({"youtube": {"player_client": client}} if client else {})}
        if "js_runtimes" in opts:
            po["js_runtimes"] = opts["js_runtimes"]
        try:
            with yt_dlp.YoutubeDL(po) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:  # noqa: BLE001
            probe_err = e
    if info is None:
        raise _classify(probe_err)
    vid = info.get("id") or re.sub(r"\W+", "_", url)[-40:]
    cached = DL_DIR / f"{vid}.mp4"
    meta_path = DL_DIR / f"{vid}.meta.json"
    meta = {
        "id": vid, "title": info.get("title") or url, "channel": info.get("channel") or info.get("uploader") or "",
        "duration": float(info.get("duration") or 0), "thumbnail_url": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or url, "upload_date": info.get("upload_date") or "",
    }
    if cached.exists() and cached.stat().st_size > 0:
        meta["path"] = str(cached)
        meta_path.write_text(json.dumps(meta))
        return meta

    # each attempt tries a different YouTube player client; a challenge usually hits only some of them
    clients = [None, ["tv"], ["mweb"], ["web_safari"], ["android_vr"]]
    attempts: list[tuple[str, Exception]] = []
    ok = False
    for attempt, client in enumerate(clients):
        o = dict(opts)
        name = client[0] if client else "default"
        if client:
            o["extractor_args"] = {**xargs, "youtube": {"player_client": client}}
            if client == ["android_vr"]:
                o.pop("cookiefile", None)  # this client refuses cookies
        for relaxed in (False, True):
            if relaxed:
                o = {**o, "format": "best", "merge_output_format": None}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    ydl.download([url])
                ok = True
                break
            except Exception as e:  # noqa: BLE001
                attempts.append((name + (" (any format)" if relaxed else ""), e))
                if not (not relaxed and _is_format_problem(e)):
                    break  # only a format problem is worth an immediate retry on the same client
        if ok:
            break
        time.sleep(1.0 * (attempt + 1))
    if not ok:
        db.log_error("download", "; ".join(f"{n}: {str(e)[:200]}" for n, e in attempts))
        raise _classify(_best_error(attempts))

    files = sorted(DL_DIR.glob(f"{vid}.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not files:
        raise DownloadBlocked("The download finished but no video file was found. Try uploading the file instead.")
    path = files[0]
    if path.suffix.lower() != ".mp4":
        media.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-c", "copy", cached])
        path.unlink(missing_ok=True)
        path = cached
    meta["path"] = str(path)
    meta_path.write_text(json.dumps(meta))
    return meta


def _is_format_problem(e: Exception) -> bool:
    low = str(e).lower()
    return any(h in low for h in FORMAT_HINTS)


def _best_error(attempts: list) -> Exception:
    """The most useful failure to show: a real refusal beats a format complaint, which beats anything else."""
    if not attempts:
        return RuntimeError("download failed")
    for _, e in attempts:
        if any(h in str(e).lower() for h in BLOCK_HINTS):
            return e
    for _, e in attempts:
        if not _is_format_problem(e):
            return e
    return attempts[-1][1]


def _classify(e: Exception) -> Exception:
    msg = str(e)
    low = msg.lower()
    reason = re.sub(r"\s+", " ", msg.replace("ERROR:", "").strip())[:260]
    have_cookies = bool(_cookie_file())
    if _is_format_problem(e):
        return DownloadBlocked(
            "YouTube let us in but offered no video we could use for this one. It is usually a live stream, a members-only "
            f"video, or one that is still processing. Try another episode or upload the file instead. (yt-dlp said: {reason})")
    if any(h in low for h in BLOCK_HINTS):
        pot = " The token helper is set." if env("POT_PROVIDER_URL") else " Adding the token helper (POT_PROVIDER_URL, see DEPLOY.md) usually fixes this."
        tip = ("Cookies are set but YouTube still refused." + pot + " Export fresh cookies from a logged-in spare account and try again, "
               "or upload the video file instead." if have_cookies else
               "Add a cookies file (YTDLP_COOKIES_B64, see DEPLOY.md) or upload the video file instead.")
        return DownloadBlocked(f"YouTube blocked the download from this server. {tip} (yt-dlp said: {reason})")
    return DownloadBlocked(f"Could not download the video. You can upload the file instead. (yt-dlp said: {reason})")


def import_upload(src: Path, project_dir: Path, title: str = "") -> dict:
    project_dir.mkdir(parents=True, exist_ok=True)
    dest = project_dir / "source.mp4"
    ext = src.suffix.lower()
    from .config import UPLOADS
    is_upload = str(src.resolve()).startswith(str(UPLOADS.resolve()))
    if ext == ".mp4" and is_upload:
        shutil.move(str(src), dest)
    elif ext == ".mp4":
        # a local file given on the command line: link it, never move the user's file
        try:
            os.link(src, dest)
        except OSError:
            os.symlink(src.resolve(), dest)
    else:
        try:
            media.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dest])
        except Exception:
            media.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                       "-c:a", "aac", dest])
        if is_upload:
            src.unlink(missing_ok=True)
    info = media.probe(dest)
    return {"id": project_dir.name, "path": str(dest), "title": title or src.stem, "channel": "",
            "duration": info["duration"], "thumbnail_url": "", "webpage_url": ""}
