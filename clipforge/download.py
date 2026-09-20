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
FORMAT_HINTS = ("requested format is not available", "no video formats found", "no formats found",
                "only images are available")
# cookies that YouTube has rotated out from under us: they make things worse, not better
COOKIE_DEAD_HINTS = ("cookies are no longer valid", "have likely been rotated", "cookies have been rotated")


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
    opts.update(_js_opts())
    opts.update(_net_opts())
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
        # the page lookup needs the same treatment as the download: solver, token helper and proxy
        po = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
              **_js_opts(), **_net_opts(), **({"cookiefile": ck} if ck else {})}
        if xargs or client:
            po["extractor_args"] = {**xargs, **({"youtube": {"player_client": client}} if client else {})}
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
    cookies_ok = bool(ck)
    ok = False
    for i, client in enumerate(clients):
        base = dict(opts)
        name = client[0] if client else "default"
        if client:
            base["extractor_args"] = {**xargs, "youtube": {"player_client": client}}
        if client == ["android_vr"]:
            base.pop("cookiefile", None)  # this client refuses cookies
        # with cookies first (when we still trust them), then without: rotated cookies break downloads
        cookie_modes = [True, False] if (cookies_ok and base.get("cookiefile")) else [False]
        for use_cookies in cookie_modes:
            o = dict(base)
            if not use_cookies:
                o.pop("cookiefile", None)
            label = name + ("" if use_cookies else " without cookies")
            for relaxed in (False, True):
                if relaxed:
                    o = {**o, "format": "best", "merge_output_format": None}
                try:
                    _fetch(o, url, proxy_is_metadata_only())
                    ok = True
                    break
                except Exception as e:  # noqa: BLE001
                    attempts.append((label + (" (any format)" if relaxed else ""), e))
                    if _cookies_are_dead(e):
                        cookies_ok = False
                    if not (not relaxed and _is_format_problem(e)):
                        break  # only a format problem is worth an immediate retry on the same client
            if ok:
                break
        if ok:
            break
        time.sleep(1.0 * (i + 1))
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


NOISE = ("retrying (", "please report this issue", "deprecated feature", "you have asked for")
INTERESTING = ("sabr", "missing a url", "po token", "pot", "sign in", "cookie", "skipped", "throttl", "format")


class _Collect:
    """Captures yt-dlp's own warnings so the app can show why formats disappeared."""

    def __init__(self):
        self.lines: list[str] = []

    def saw_po_token(self) -> bool:
        """Did the token helper actually hand yt-dlp a token for this request?"""
        return any(("po token" in l.lower() and "fetch" in l.lower()) or "pot:" in l.lower()
                   or "bgutilhttp" in l.lower() for l in self.lines)

    def notable(self, limit: int = 3) -> list[str]:
        """The warnings worth reading: no retry noise, no duplicates, most telling first."""
        seen, out = set(), []
        for kind in (True, False):  # interesting ones first
            for l in self.lines:
                if not l.startswith("WARNING"):
                    continue
                low = l.lower()
                if any(n in low for n in NOISE):
                    continue
                if (any(i in low for i in INTERESTING)) != kind:
                    continue
                key = low[:80]
                if key in seen:
                    continue
                seen.add(key)
                out.append(l.replace("WARNING: ", "")[:220])
        return out[:limit]

    def debug(self, m):
        if m.startswith("[debug] "):
            return
        self.lines.append(m)

    def info(self, m):
        self.lines.append(m)

    def warning(self, m):
        self.lines.append("WARNING: " + m)

    def error(self, m):
        self.lines.append("ERROR: " + m)


def pot_provider_status() -> dict:
    """Is the proof-of-origin token helper set up and reachable?"""
    url = env("POT_PROVIDER_URL")
    out = {"url": url, "set": bool(url), "reachable": False, "detail": "", "plugin": False}
    try:
        import yt_dlp as _y
        _y.YoutubeDL({"quiet": True, "no_warnings": True})  # loading a client loads the plugins
        from yt_dlp.extractor.youtube.pot._registry import _pot_providers
        out["plugin"] = "BgUtilHTTP" in _pot_providers.value
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"plugin check failed: {e}"
    if not url:
        return out
    import httpx
    for path in ("/ping", "/"):
        try:
            r = httpx.get(url.rstrip("/") + path, timeout=8)
            out["reachable"] = r.status_code < 500
            out["detail"] = f"{path} -> {r.status_code}"
            if out["reachable"]:
                break
        except Exception as e:  # noqa: BLE001
            out["detail"] = str(e)[:200]
    return out


def proxy_status() -> dict:
    """Is the proxy set, and does it actually work? Reports the address YouTube would see."""
    p = proxy_url()
    out = {"set": bool(p), "working": False, "ip": "", "detail": "", "metadata_only": proxy_is_metadata_only()}
    if not p:
        return out
    import httpx
    try:
        r = httpx.get("https://api.ipify.org?format=json", proxy=p, timeout=20)
        r.raise_for_status()
        out["ip"] = (r.json() or {}).get("ip", "")
        out["working"] = bool(out["ip"])
        out["detail"] = "traffic goes out from " + out["ip"]
    except Exception as e:  # noqa: BLE001
        out["detail"] = str(e)[:200]
    return out


def diagnose(url: str) -> dict:
    """What the server can actually see for this link: cookies, token helper, and the formats offered."""
    import yt_dlp
    ck = _cookie_file()
    res = {"url": url, "cookies": bool(ck), "pot": pot_provider_status(), "js": js_solver_status(),
           "proxy": proxy_status(), "clients": [], "formats": [], "title": "", "warnings": []}
    xargs = {}
    pot = env("POT_PROVIDER_URL")
    if pot:
        xargs["youtubepot-bgutilhttp"] = {"base_url": [pot.rstrip("/")]}
    plans = [(None, True), (["tv"], True), (None, False), (["tv"], False), (["mweb"], True), (["web_safari"], True), (["android_vr"], False)]
    if not ck:
        plans = [(c, False) for c, _ in plans if not (c is None and _ is False)] or [(None, False)]
    for client, use_cookies in plans:
        log = _Collect()
        o = {"quiet": True, "no_warnings": False, "skip_download": True, "noplaylist": True, "logger": log,
             "verbose": True, **_js_opts(), **_net_opts()}
        if ck and use_cookies:
            o["cookiefile"] = ck
        ea = dict(xargs)
        if client:
            ea["youtube"] = {"player_client": client}
        if ea:
            o["extractor_args"] = ea
        name = (client[0] if client else "default") + ("" if use_cookies else " without cookies")
        try:
            with yt_dlp.YoutubeDL(o) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = [f for f in (info.get("formats") or []) if f.get("url")]
            video = [f for f in fmts if (f.get("vcodec") or "none") != "none"]
            res["title"] = res["title"] or info.get("title") or ""
            res["clients"].append({"client": name, "ok": True, "formats": len(fmts), "video_formats": len(video),
                                   "best": max([f.get("height") or 0 for f in video] or [0]),
                                   "token": log.saw_po_token(), "warnings": log.notable()})
            if not res["formats"]:
                res["formats"] = [f"{f.get('format_id')} {f.get('ext')} {f.get('height') or ''}p" for f in video[-8:]]
        except Exception as e:  # noqa: BLE001
            res["clients"].append({"client": name, "ok": False, "error": _short_error(e),
                                   "token": log.saw_po_token(), "warnings": log.notable()})
        res["warnings"] += log.notable()
    res["verdict"] = _verdict(res)
    return res


def _short_error(e: Exception) -> str:
    """yt-dlp errors carry a lot of boilerplate; keep the part that says what went wrong."""
    msg = re.sub(r"\s+", " ", str(e))
    msg = re.sub(r"; please report this issue.*", "", msg)
    msg = re.sub(r"\s*\(caused by .*", "", msg)
    return msg.strip()[:240]


def _verdict(res: dict) -> str:
    good = [c for c in res["clients"] if c.get("ok") and c.get("video_formats")]
    if good:
        extra = ""
        if "without cookies" in good[0]["client"] and res.get("cookies"):
            extra = " Your saved cookies are getting in the way: remove YTDLP_COOKIES_B64 and links will work."
        return (f"Downloads should work: {good[0]['client']} offers {good[0]['video_formats']} video formats "
                f"up to {good[0]['best']}p.{extra}")
    if not res.get("js", {}).get("ok"):
        js = res.get("js", {})
        miss = "no JavaScript runtime (deno) in this build" if not js.get("runtime") else "the yt-dlp-ejs solver package is missing"
        return f"YouTube's JavaScript challenge cannot be solved here: {miss}. Redeploy so the image installs it."
    if any(_cookies_are_dead(Exception(w)) for w in res.get("warnings", [])):
        return ("Your saved YouTube cookies have been rotated and now block downloads. Remove YTDLP_COOKIES_B64 "
                "(with the token helper and solver in place, cookies are usually not needed) or export fresh ones.")
    blocked = [c for c in res["clients"] if not c.get("ok") and any(h in (c.get("error") or "").lower() for h in BLOCK_HINTS)]
    if blocked:
        got_token = any(c.get("token") for c in res["clients"])
        if res.get("proxy", {}).get("set") and not res["proxy"].get("working"):
            return ("The proxy is set but not working: " + res["proxy"].get("detail", "") +
                    ". Check the address, port, username and password from your proxy dashboard.")
        if res.get("cookies"):
            return ("YouTube is refusing this server even though the solver and token helper are working. First remove "
                    "YTDLP_COOKIES_B64 and check again: stale cookies are the usual cause. If it still refuses, this "
                    "server's IP address is the problem, and a proxy (YTDLP_PROXY) or uploading the file is the way "
                    "through." + ("" if got_token else " (No proof-of-origin token was fetched for these requests.)"))
        return ("YouTube is refusing this server's IP address. Everything on this side is set up correctly"
                f"{' and tokens are being fetched' if got_token else ', but no proof-of-origin token was fetched'}. "
                "Set YTDLP_PROXY to a residential proxy, or upload episode files instead.")
    saw_sabr = any("sabr" in w.lower() or "missing a url" in w.lower() for w in res["warnings"])
    if not res["pot"]["set"]:
        return ("No formats came back and the token helper is not set. Add the bgutil service and POT_PROVIDER_URL "
                "(DEPLOY.md, Railway step 6).")
    if not res["pot"]["plugin"]:
        return "The bgutil plugin is missing from this build. Redeploy so requirements.txt is installed again."
    if not res["pot"]["reachable"]:
        return (f"The token helper at {res['pot']['url']} cannot be reached ({res['pot']['detail']}). "
                "Check the bgutil service is deployed and the URL matches its private name and port 4416.")
    if saw_sabr:
        return ("YouTube is holding back the video streams even with the token helper. Fresh cookies from a "
                "logged-in account usually fix this.")
    return "No client returned a usable video. See the per-client errors below."


def _is_format_problem(e: Exception) -> bool:
    low = str(e).lower()
    return any(h in low for h in FORMAT_HINTS)


def _cookies_are_dead(e: Exception) -> bool:
    low = str(e).lower()
    return any(h in low for h in COOKIE_DEAD_HINTS)


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


def proxy_url() -> str:
    """An optional proxy for YouTube only (YTDLP_PROXY). Datacenter IPs get blocked; a residential proxy fixes it."""
    p = (env("YTDLP_PROXY") or env("YOUTUBE_PROXY")).strip().rstrip("/")
    if p and "://" not in p:
        p = "http://" + p
    return p


def _net_opts() -> dict:
    p = proxy_url()
    return {"proxy": p} if p else {}


def proxy_is_metadata_only() -> bool:
    """A proxy is only needed for YouTube's small API calls: the video files come from Google's CDN, which does
    not care about the server's address. Keeping the big download off the proxy is what makes this affordable."""
    return bool(proxy_url()) and bool(cfg.get("download.proxy_metadata_only", True))


def _fetch(o: dict, url: str, split: bool) -> None:
    """One download attempt. With `split`, the page is read through the proxy and the video is fetched directly."""
    import yt_dlp
    if not split:
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.download([url])
        return
    with yt_dlp.YoutubeDL({**o, "proxy": proxy_url()}) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    direct = {k: v for k, v in o.items() if k != "proxy"}
    tmp = DL_DIR / f"info_{info.get('id', 'video')}.info.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(info))
    try:
        with yt_dlp.YoutubeDL(direct) as ydl:
            ydl.download_with_info_file(str(tmp))
    except Exception:
        # some videos hand out address-bound links: fall back to pulling it all through the proxy
        with yt_dlp.YoutubeDL({**o, "proxy": proxy_url()}) as ydl:
            ydl.download_with_info_file(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def _js_opts() -> dict:
    """YouTube hides its streams behind a JavaScript challenge. Solving it needs a JS runtime plus the
    solver scripts (the yt-dlp-ejs package ships them; 'remote components' is the fallback)."""
    out: dict = {}
    for rt in ("deno", "node", "bun"):
        if shutil.which(rt):
            out["js_runtimes"] = {rt: {}}
            break
    allow = cfg.get("download.remote_components", ["ejs:github", "ejs:npm"])
    if allow:
        out["remote_components"] = list(allow)
    return out


def js_solver_status() -> dict:
    """Can this server solve YouTube's JS challenge?"""
    rt = next((r for r in ("deno", "node", "bun") if shutil.which(r)), "")
    try:
        import importlib.metadata as md
        ejs = md.version("yt-dlp-ejs")
    except Exception:  # noqa: BLE001
        ejs = ""
    return {"runtime": rt, "ejs": ejs, "ok": bool(rt) and bool(ejs),
            "remote": list(cfg.get("download.remote_components", []) or [])}


def _classify(e: Exception) -> Exception:
    msg = str(e)
    low = msg.lower()
    reason = re.sub(r"\s+", " ", msg.replace("ERROR:", "").strip())[:260]
    have_cookies = bool(_cookie_file())
    js = js_solver_status()
    if _cookies_are_dead(e):
        return DownloadBlocked(
            "Your saved YouTube cookies have been rotated by YouTube and no longer work. Either export fresh ones "
            "(from a private window, then close it straight away) or simply remove YTDLP_COOKIES_B64: with the token "
            f"helper and the challenge solver in place, downloads usually work without cookies. (yt-dlp said: {reason})")
    if _is_format_problem(e):
        if not js["ok"]:
            missing = "a JavaScript runtime (deno)" if not js["runtime"] else "the yt-dlp-ejs solver package"
            return DownloadBlocked(
                f"YouTube held back every video stream because this server cannot solve its JavaScript challenge: "
                f"{missing} is missing. Redeploy so the image installs it. (yt-dlp said: {reason})")
        return DownloadBlocked(
            "YouTube answered but held back every video stream. Open the Status page and run 'Check a YouTube link' "
            "on this URL: it says whether the token helper and the challenge solver are working. "
            f"Uploading the file always works. (yt-dlp said: {reason})")
    if any(h in low for h in BLOCK_HINTS):
        if have_cookies:
            tip = ("Your saved cookies are the most likely cause: remove YTDLP_COOKIES_B64 and try again "
                   "(the token helper and challenge solver work without them).")
        elif not proxy_url():
            tip = ("This server's IP address is blocked by YouTube. Set YTDLP_PROXY to a residential proxy, "
                   "or upload the episode file instead.")
        else:
            tip = "Even through the proxy YouTube refused. Try another proxy, or upload the episode file instead."
        return DownloadBlocked(f"YouTube blocked the download from this server. {tip} "
                               f"Run 'Check a YouTube link' on the Status page for the full picture. (yt-dlp said: {reason})")
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
