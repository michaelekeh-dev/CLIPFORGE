"""Get the source video: yt-dlp for links, plain copy for uploads. Cached by video id."""
from __future__ import annotations
import json
import os
import re
import shutil
import time
from pathlib import Path
from . import media
from .config import CACHE, cfg, env

DL_DIR = CACHE / "downloads"


class DownloadBlocked(Exception):
    """YouTube (or the site) refused the download. The app offers an upload instead."""


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip(), re.I))


BLOCK_HINTS = ("sign in to confirm", "not a bot", "403", "forbidden", "unable to download", "login required",
               "private video", "this video is unavailable", "blocked", "captcha", "429", "too many requests",
               "requested format is not available", "unable to extract")


def _cookie_file() -> str | None:
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
           f"b[height<={max_h}][ext=mp4]/b[height<={max_h}]/b")

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

    # Cached?
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                               **({"cookiefile": ck} if ck else {})}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise _classify(e)
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

    last = None
    for attempt in range(int(cfg.get("download.retries", 3))):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    else:
        raise _classify(last)

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


def _classify(e: Exception) -> Exception:
    msg = str(e)
    low = msg.lower()
    if any(h in low for h in BLOCK_HINTS):
        return DownloadBlocked(
            "YouTube blocked the download from this server. Add a cookies file (YTDLP_COOKIES, see DEPLOY.md) "
            "or upload the video file instead.")
    return DownloadBlocked("Could not download the video: " + msg[:300] + ". You can upload the file instead.")


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
