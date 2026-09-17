"""B-roll: when a clip mentions something visual, show a short free Pexels video full-frame with captions on top."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
from pathlib import Path
import numpy as np
import cv2
from .config import cfg, env, CACHE
from .timeline import Timeline

B = cfg.get("broll")
BROLL_CACHE = CACHE / "broll"

VISUAL_WORDS = {
    "pyramids": "egypt pyramids", "pyramid": "egypt pyramids", "egypt": "egypt desert", "desert": "desert", "ocean": "ocean waves",
    "sea": "ocean waves", "mountain": "mountains", "mountains": "mountains", "forest": "forest trees", "jungle": "jungle",
    "city": "city skyline", "space": "outer space stars", "stars": "night sky stars", "moon": "moon night", "sun": "sunrise",
    "rain": "rain", "storm": "thunderstorm", "lightning": "lightning storm", "fire": "fire flames", "church": "church interior",
    "bible": "open bible", "cross": "wooden cross sunset", "jerusalem": "jerusalem old city", "rome": "rome colosseum",
    "israel": "jerusalem", "temple": "ancient temple", "ancient": "ancient ruins", "ruins": "ancient ruins", "castle": "castle",
    "war": "soldiers war", "army": "soldiers marching", "money": "money cash", "gold": "gold bars", "computer": "computer code screen",
    "phone": "smartphone hands", "internet": "server room", "robot": "robot", "ai": "artificial intelligence",
    "brain": "brain neurons", "heart": "heartbeat", "hospital": "hospital corridor", "doctor": "doctor", "car": "car driving",
    "plane": "airplane sky", "airport": "airport", "train": "train", "ship": "ship sea", "river": "river", "lake": "lake",
    "farm": "farm field", "food": "food cooking", "coffee": "coffee cup", "school": "classroom", "book": "old book pages",
    "library": "library books", "clock": "clock time", "map": "world map", "earth": "planet earth space", "world": "planet earth",
    "sky": "clouds sky timelapse", "clouds": "clouds timelapse", "night": "city night", "crowd": "crowd people", "stadium": "stadium crowd",
    "baby": "baby", "kids": "children playing", "dog": "dog", "cat": "cat", "lion": "lion", "snake": "snake", "bird": "birds flying",
    "chimpanzee": "chimpanzee", "chimpanzees": "chimpanzees", "monkey": "monkey", "animals": "wild animals", "cake": "birthday cake",
    "party": "party celebration", "street": "busy street", "road": "road highway", "bridge": "bridge", "building": "skyscraper",
    "machines": "industrial machines", "machine": "industrial machine", "factory": "factory", "flowers": "flowers field",
    "waterfall": "waterfall", "waterfalls": "waterfall", "snow": "snow falling", "ice": "ice glacier", "beach": "beach waves",
}


def suggestions(data: dict, words: list[dict], start: float, end: float) -> list[dict]:
    """[{t, query, word}] from Claude's picks or the visual-word list. Spread out, not in the first seconds."""
    out = []
    for s in data.get("broll") or []:
        try:
            t = float(s["t"])
            q = str(s["query"]).strip()
            if q and start <= t <= end:
                out.append({"t": t, "query": q, "word": s.get("word", "")})
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        for w in words:
            base = re.sub(r"[^a-zA-Z']", "", w["w"]).lower()
            q = VISUAL_WORDS.get(base)
            if q and start <= w["s"] <= end:
                out.append({"t": w["s"], "query": q, "word": w["w"]})
    out.sort(key=lambda s: s["t"])
    picked: list[dict] = []
    for s in out:
        if s["t"] - start < float(B.get("not_before", 3.0)):
            continue
        if picked and s["t"] - picked[-1]["t"] < float(B.get("min_gap", 6.0)):
            continue
        if end - s["t"] < float(B.get("max_seconds", 2.5)) + 0.5:
            continue
        picked.append(s)
        if len(picked) >= int(B.get("max_per_clip", 2)):
            break
    return picked


# ----------------------------------------------------------------------------- pexels
def _key() -> str:
    return env("PEXELS_API_KEY")


def search_video(query: str, portrait: bool = True) -> dict | None:
    """Best matching Pexels video file for the query (cached). Returns {id, url, width, height, user, page} or None."""
    mock = env("PEXELS_MOCK_DIR")
    if mock:
        files = sorted(Path(mock).glob("*.mp4"))
        if not files:
            return None
        f = files[int(hashlib.sha1(query.encode()).hexdigest(), 16) % len(files)]
        return {"id": f.stem, "url": str(f), "width": 0, "height": 0, "user": "mock", "page": ""}
    if not _key():
        return None
    import httpx
    BROLL_CACHE.mkdir(parents=True, exist_ok=True)
    ck = BROLL_CACHE / ("search_" + hashlib.sha1(f"{query}:{portrait}".encode()).hexdigest()[:16] + ".json")
    if ck.exists():
        try:
            return json.loads(ck.read_text()) or None
        except Exception:
            pass
    try:
        r = httpx.get("https://api.pexels.com/videos/search", params={"query": query, "per_page": int(B.get("per_page", 5)),
                      "orientation": "portrait" if portrait else "landscape", "size": "medium"},
                      headers={"Authorization": _key()}, timeout=20)
        r.raise_for_status()
        best = None
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("width")]
            if not files:
                continue
            # prefer around 1080 wide, small enough to download quickly
            f = min(files, key=lambda f: abs((f.get("height") or 0) - 1920) if portrait else abs((f.get("width") or 0) - 1920))
            cand = {"id": v["id"], "url": f["link"], "width": f.get("width"), "height": f.get("height"),
                    "user": (v.get("user") or {}).get("name", ""), "page": v.get("url", ""), "duration": v.get("duration", 0)}
            if best is None or (cand["duration"] or 0) >= 4:
                best = cand
                if (cand["duration"] or 0) >= 4:
                    break
        ck.write_text(json.dumps(best or {}))
        return best
    except Exception:
        return None


def fetch(video: dict) -> Path | None:
    """Download (cached) the video file."""
    if not video:
        return None
    if env("PEXELS_MOCK_DIR"):
        return Path(video["url"])
    BROLL_CACHE.mkdir(parents=True, exist_ok=True)
    p = BROLL_CACHE / f"pexels_{video['id']}.mp4"
    if p.exists() and p.stat().st_size > 0:
        return p
    try:
        import httpx
        with httpx.stream("GET", video["url"], timeout=60, follow_redirects=True) as r:
            r.raise_for_status()
            with open(p, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        return p
    except Exception:
        p.unlink(missing_ok=True)
        return None


# ----------------------------------------------------------------------------- planning + frames
def plan(data: dict, words: list[dict], tl: Timeline, settings: dict, ratio: str = "9:16") -> list[dict]:
    """Decide the B-roll shots for a clip. Editor overrides in settings['broll_items'] (query swap / removed)."""
    if not (settings.get("broll", True) and B.get("enabled", True)):
        return []
    if not (_key() or env("PEXELS_MOCK_DIR")):
        return []
    sugg = suggestions(data, words, tl.start, tl.end)
    overrides = settings.get("broll_items") or {}
    items = []
    dur = float(B.get("max_seconds", 2.5))
    for idx, s in enumerate(sugg):
        ov = overrides.get(str(idx)) or {}
        if ov.get("removed"):
            continue
        query = (ov.get("query") or s["query"]).strip()
        t_out = tl.to_output(s["t"])
        if t_out is None:
            continue
        v = search_video(query, portrait=(ratio == "9:16"))
        path = fetch(v) if v else None
        if not path:
            continue
        s_out = round(t_out - 0.15, 3)
        items.append({"idx": idx, "t": s["t"], "word": s.get("word", ""), "query": query, "s": s_out, "e": round(s_out + dur, 3),
                      "video": str(path), "credit": f"Pexels / {v.get('user', '')}".strip(" /"), "page": v.get("page", "")})
    return items


class BrollFrames:
    """Replaces output frames with B-roll frames during the planned windows (fitted to the output size)."""

    def __init__(self, items: list[dict], out_w: int, out_h: int, fps: float):
        self.items = []
        self.ow, self.oh, self.fps = out_w, out_h, fps
        for it in items:
            frames = self._load(it["video"], it["e"] - it["s"])
            if frames:
                self.items.append({**it, "frames": frames})

    def _load(self, path: str, seconds: float) -> list[np.ndarray]:
        from . import media
        try:
            info = media.probe(path)
        except Exception:
            return []
        if not info["width"]:
            return []
        start = max(0.0, min(1.0, (info["duration"] - seconds) / 2))
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", "-t", f"{seconds + 0.1:.2f}", "-i", path,
               "-vf", f"fps={self.fps},scale={self.ow}:{self.oh}:force_original_aspect_ratio=increase,crop={self.ow}:{self.oh}",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 7)
        size = self.ow * self.oh * 3
        frames = []
        n = int(seconds * self.fps) + 1
        try:
            while len(frames) < n:
                buf = p.stdout.read(size)
                if len(buf) < size:
                    break
                frames.append(np.frombuffer(buf, dtype=np.uint8).reshape(self.oh, self.ow, 3).copy())
        finally:
            p.stdout.close()
            p.wait()
        return frames

    def __call__(self, frame: np.ndarray, t_src: float, t_out: float) -> np.ndarray:
        for it in self.items:
            if it["s"] <= t_out < it["e"]:
                k = int((t_out - it["s"]) * self.fps)
                fr = it["frames"][min(k, len(it["frames"]) - 1)]
                # short cross-dissolve at both ends so it never pops
                edge = 0.18
                a = min(1.0, (t_out - it["s"]) / edge, (it["e"] - t_out) / edge)
                if a >= 1.0:
                    return fr.copy()
                return cv2.addWeighted(fr, a, frame, 1 - a, 0)
        return frame
