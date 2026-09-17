"""Smart reframe: camera cuts -> faces per shot -> layout (single / split / wide) -> steady, eased crops.

analyze() produces a JSON-able dict (cached), SmartFramer turns it into output frames.
"""
from __future__ import annotations
import json
import math
import subprocess
from pathlib import Path
import numpy as np
import cv2
from .config import cfg, ASSETS, env
from .timeline import Timeline

YUNET = ASSETS / "models" / "face_detection_yunet_2023mar.onnx"
LANDMARKER = ASSETS / "models" / "face_landmarker.task"
R = cfg.get("reframe")


# ----------------------------------------------------------------------------- shots
def detect_shots(source: Path, start: float, end: float) -> list[tuple[float, float]]:
    """Camera cuts inside [start, end] via PySceneDetect. Falls back to one shot."""
    try:
        from scenedetect import open_video, SceneManager, ContentDetector
        video = open_video(str(source))
        video.seek(max(0.0, start))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=float(R.get("scene_threshold", 27)), min_scene_len=int(video.frame_rate * float(R.get("min_shot_seconds", 0.6)))))
        sm.detect_scenes(video, end_time=end, show_progress=False)
        scenes = sm.get_scene_list()
        cuts = [s[0].get_seconds() for s in scenes[1:]]
    except Exception:
        cuts = []
    bounds = [start] + [c for c in cuts if start + 0.2 < c < end - 0.2] + [end]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if shots and b - a < float(R.get("min_shot_seconds", 0.6)):
            shots[-1] = (shots[-1][0], b)
        else:
            shots.append((a, b))
    return shots


# ----------------------------------------------------------------------------- faces
class FaceFinder:
    def __init__(self, w: int, h: int):
        self.det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.6, 0.3, 50)
        self.w, self.h = w, h

    def __call__(self, img: np.ndarray) -> list[list[float]]:
        n, faces = self.det.detect(img)
        out = []
        if faces is None:
            return out
        for f in faces:
            x, y, w, h = [float(v) for v in f[:4]]
            lm = [float(v) for v in f[4:14]]
            out.append([x, y, w, h, float(f[14])] + lm)
        return out


class LipReader:
    """Mouth openness from MediaPipe face landmarks on a face crop."""

    def __init__(self):
        self.ok = False
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision
            self.mp = mp
            self.lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                base_options=mpp.BaseOptions(model_asset_path=str(LANDMARKER)), num_faces=1,
                min_face_detection_confidence=0.3, min_face_presence_confidence=0.3))
            self.ok = True
        except Exception:
            self.ok = False

    def openness(self, crop_bgr: np.ndarray) -> float | None:
        if not self.ok or crop_bgr.size == 0:
            return None
        try:
            img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))
            res = self.lm.detect(img)
            if not res.face_landmarks:
                return None
            pts = res.face_landmarks[0]
            top, bot = pts[13], pts[14]        # inner upper / lower lip
            chin, brow = pts[152], pts[10]     # face height reference
            fh = math.hypot(chin.x - brow.x, chin.y - brow.y) or 1e-6
            return math.hypot(top.x - bot.x, top.y - bot.y) / fh
        except Exception:
            return None

    def close(self):
        try:
            if self.ok:
                self.lm.close()
        except Exception:
            pass


def _decode_small(source: Path, start: float, end: float, fps: float, width: int):
    """Yield (t, frame) at reduced size."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{max(0.0, end - start):.3f}", "-i", str(source),
           "-vf", f"fps={fps},scale={width}:-2", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 7)
    # find height from first read: probe once
    from . import media
    info = media.probe(source)
    h = int(round(width * info["height"] / info["width"] / 2) * 2)
    size = width * h * 3
    i = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield start + i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(h, width, 3)
            i += 1
    finally:
        p.stdout.close()
        p.wait()


def analyze(source: Path, start: float, end: float, workdir: Path, audio_wav: Path | None = None, progress=None) -> dict:
    """Shots, face tracks, layouts and speaker turns for [start, end]. Cached in workdir."""
    workdir.mkdir(parents=True, exist_ok=True)
    cache = workdir / f"reframe_{start:.2f}_{end:.2f}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    from . import media
    info = media.probe(source)
    W, H = info["width"], info["height"]
    fps = float(R.get("sample_fps", 5))
    dw = int(R.get("detect_width", 640))
    scale = W / dw
    shots = detect_shots(source, start, end)
    if progress:
        progress("Finding faces", 5)
    finder = FaceFinder(dw, int(round(dw * H / W / 2) * 2))
    lips = LipReader()
    frames: list[dict] = []  # per sampled frame: t, faces (in source px)
    total = max(1, int((end - start) * fps))
    for k, (t, img) in enumerate(_decode_small(source, start, end, fps, dw)):
        faces = finder(img)
        rec = []
        for f in faces:
            x, y, w, h, sc = f[:5]
            if h * scale < float(R.get("min_face_frac", 0.06)) * H:
                continue
            mouth = None
            if lips.ok:
                pad = 0.25
                x0, y0 = int(max(0, x - pad * w)), int(max(0, y - pad * h))
                x1, y1 = int(min(img.shape[1], x + w * (1 + pad))), int(min(img.shape[0], y + h * (1 + pad)))
                crop = img[y0:y1, x0:x1]
                if crop.shape[0] > 20 and crop.shape[1] > 20:
                    if crop.shape[0] < 192:
                        crop = cv2.resize(crop, (int(crop.shape[1] * 192 / crop.shape[0]), 192))
                    mouth = lips.openness(crop)
            rec.append({"x": x * scale, "y": y * scale, "w": w * scale, "h": h * scale, "score": sc, "mouth": mouth})
        frames.append({"t": round(t, 3), "faces": rec})
        if progress and k % 25 == 0:
            progress("Finding faces", 5 + 60 * k / total)
    lips.close()

    audio_rms = _audio_rms(audio_wav, start, end) if audio_wav else None
    out_shots = []
    for (a, b) in shots:
        fr = [f for f in frames if a - 0.5 / fps <= f["t"] < b]
        tracks = build_tracks(fr, W, H)
        n_frames = max(1, len(fr))
        tracks = [t for t in tracks if t["coverage"] >= float(R.get("min_coverage", 0.4))]
        tracks.sort(key=lambda t: -t["size"])
        layout = choose_layout(tracks, W, H)
        speaker = speaker_turns(tracks, a, b, audio_rms)
        out_shots.append({"start": round(a, 3), "end": round(b, 3), "layout": layout, "tracks": tracks, "speaker": speaker})
    pyannote_turns = diarize(audio_wav, start, end) if audio_wav else None
    if pyannote_turns:
        for sh in out_shots:
            sh["speaker"] = merge_diarization(sh, pyannote_turns)
    result = {"start": start, "end": end, "width": W, "height": H, "sample_fps": fps, "shots": out_shots,
              "lip_reader": lips.ok, "diarization": bool(pyannote_turns)}
    cache.write_text(json.dumps(result))
    return result


def build_tracks(frames: list[dict], W: int, H: int) -> list[dict]:
    """Group detections across frames of one shot into tracks by position."""
    tracks: list[dict] = []
    for fr in frames:
        for f in fr["faces"]:
            cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
            best, bd = None, 1e9
            for tr in tracks:
                d = math.hypot(cx - tr["cx"], cy - tr["cy"]) / max(tr["h"], 1)
                if d < 1.0 and d < bd:
                    best, bd = tr, d
            if best is None:
                best = {"cx": cx, "cy": cy, "h": f["h"], "pts": []}
                tracks.append(best)
            best["pts"].append([fr["t"], cx, cy, f["w"], f["h"], f["mouth"] if f["mouth"] is not None else -1])
            n = len(best["pts"])
            best["cx"] = (best["cx"] * (n - 1) + cx) / n
            best["cy"] = (best["cy"] * (n - 1) + cy) / n
            best["h"] = (best["h"] * (n - 1) + f["h"]) / n
    out = []
    n_frames = max(1, len(frames))
    for i, tr in enumerate(tracks):
        pts = np.array(tr["pts"], dtype=float)
        out.append({
            "id": i, "cx": float(np.median(pts[:, 1])), "cy": float(np.median(pts[:, 2])),
            "w": float(np.median(pts[:, 3])), "h": float(np.median(pts[:, 4])),
            "size": float(np.median(pts[:, 4])) / H, "coverage": len(pts) / n_frames,
            "pts": [[round(float(v), 3) for v in p] for p in tr["pts"]],
        })
    return out


def choose_layout(tracks: list[dict], W: int, H: int) -> str:
    n = len(tracks)
    if n == 1:
        return "single"
    if n == 2:
        a, b = tracks
        # both faces must be real (not a tiny one in the background) and clearly apart
        if b["size"] >= 0.5 * a["size"] and abs(a["cx"] - b["cx"]) > 0.18 * W:
            return "split"
        return "single"
    if n >= 3:
        return "speaker" if R.get("three_plus") == "speaker" else "wide"
    return "wide"


# ----------------------------------------------------------------------------- speaker
def _audio_rms(wav: Path | None, start: float, end: float, hop: float = 0.1) -> dict | None:
    """RMS energy every `hop` seconds of the clip audio (wav covers [start, end])."""
    if not wav or not Path(wav).exists():
        return None
    import wave
    try:
        with wave.open(str(wav)) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        n = int(hop * sr)
        m = len(data) // n
        rms = np.sqrt((data[:m * n].reshape(m, n) ** 2).mean(axis=1))
        return {"hop": hop, "start": start, "rms": rms.tolist(), "thresh": float(np.percentile(rms, 40))}
    except Exception:
        return None


def _loud(audio_rms: dict | None, t: float) -> bool:
    if not audio_rms:
        return True
    i = int((t - audio_rms["start"]) / audio_rms["hop"])
    if 0 <= i < len(audio_rms["rms"]):
        return audio_rms["rms"][i] > audio_rms["thresh"]
    return True


def speaker_turns(tracks: list[dict], a: float, b: float, audio_rms: dict | None) -> list[list]:
    """[[t0, t1, track_id, confidence], ...] covering the shot. Lip movement + loudness, switches >= 2s apart."""
    if not tracks:
        return []
    if len(tracks) == 1:
        return [[round(a, 3), round(b, 3), tracks[0]["id"], 1.0]]
    win = 0.5
    n = max(1, int(math.ceil((b - a) / win)))
    scores = np.zeros((len(tracks), n))
    for ti, tr in enumerate(tracks):
        pts = tr["pts"]
        for k in range(1, len(pts)):
            t, m0, m1 = pts[k][0], pts[k - 1][5], pts[k][5]
            if m0 < 0 or m1 < 0:
                continue
            if not _loud(audio_rms, t):
                continue
            w = min(n - 1, max(0, int((t - a) / win)))
            scores[ti, w] += abs(m1 - m0)
    # smooth over 3 windows
    if n >= 3:
        k = np.array([0.25, 0.5, 0.25])
        scores = np.array([np.convolve(row, k, mode="same") for row in scores])
    turns = []
    first = scores[:, :min(n, 4)].sum(axis=1)
    cur = int(np.argmax(first)) if first.sum() > 0 else int(np.argmax(scores.sum(axis=1)))  # who talks first
    seg_start = a
    last_switch = a - 10
    min_gap = float(R.get("min_switch_seconds", 2.0))
    for w in range(n):
        t = a + w * win
        col = scores[:, w]
        best = int(np.argmax(col))
        total = col.sum()
        conf = float(col[best] / total) if total > 0 else 0.0
        if best != cur and conf > 0.6 and t - last_switch >= min_gap:
            # require the lead to hold for the next window too
            nxt = scores[:, w + 1] if w + 1 < n else col
            if int(np.argmax(nxt)) == best:
                turns.append([round(seg_start, 3), round(t, 3), tracks[cur]["id"], 1.0])
                seg_start, cur, last_switch = t, best, t
    turns.append([round(seg_start, 3), round(b, 3), tracks[cur]["id"], 1.0])
    # confidence for the whole shot: how clearly one face moves its lips when the audio is loud
    tot = scores.sum()
    conf = float(scores.max(axis=0).sum() / tot) if tot > 0 else 0.0
    for tr in turns:
        tr[3] = round(conf, 2)
    return turns


def diarize(wav: Path | None, start: float, end: float) -> list[list] | None:
    """Optional pyannote speaker turns [[t0, t1, label]] when HF_TOKEN is set and pyannote.audio is installed."""
    if not wav or not env("HF_TOKEN"):
        return None
    try:
        from pyannote.audio import Pipeline
        pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=env("HF_TOKEN"))
        try:
            import torch
            if torch.cuda.is_available():
                pipe.to(torch.device("cuda"))
        except Exception:
            pass
        dia = pipe(str(wav))
        return [[start + seg.start, start + seg.end, label] for seg, _, label in dia.itertracks(yield_label=True)]
    except Exception:
        return None


def merge_diarization(shot: dict, turns: list[list]) -> list[list]:
    """Map pyannote labels to face tracks by lip activity overlap, then use the label turns inside the shot."""
    tracks = shot["tracks"]
    if len(tracks) < 2:
        return shot["speaker"]
    labels = sorted({t[2] for t in turns})
    act = {lab: np.zeros(len(tracks)) for lab in labels}
    for ti, tr in enumerate(tracks):
        pts = tr["pts"]
        for k in range(1, len(pts)):
            t = pts[k][0]
            if pts[k][5] < 0 or pts[k - 1][5] < 0:
                continue
            for t0, t1, lab in turns:
                if t0 <= t < t1:
                    act[lab][ti] += abs(pts[k][5] - pts[k - 1][5])
    mapping = {lab: int(np.argmax(v)) if v.sum() > 0 else None for lab, v in act.items()}
    out = []
    a, b = shot["start"], shot["end"]
    for t0, t1, lab in sorted(turns):
        s, e = max(a, t0), min(b, t1)
        if e - s < 0.3 or mapping.get(lab) is None:
            continue
        tid = tracks[mapping[lab]]["id"]
        if out and out[-1][2] == tid and s - out[-1][1] < 1.0:
            out[-1][1] = e
        else:
            out.append([round(s, 3), round(e, 3), tid, 0.9])
    if not out:
        return shot["speaker"]
    out[0][0] = a
    out[-1][1] = b
    for i in range(1, len(out)):
        out[i][0] = out[i - 1][1]
    return out


# ----------------------------------------------------------------------------- framing
class Spring:
    """Critically damped spring: smooth, no overshoot."""

    def __init__(self, x: float, tau: float):
        self.x, self.v, self.tau = x, 0.0, max(0.05, tau)

    def reset(self, x: float):
        self.x, self.v = x, 0.0

    def step(self, target: float, dt: float, tau: float | None = None) -> float:
        tau = tau or self.tau
        w = 2.0 / tau
        a = w * w * (target - self.x) - 2 * w * self.v
        self.v += a * dt
        self.x += self.v * dt
        return self.x


def hex_bgr(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


class SmartFramer:
    """Frames each source frame using the analysis. Keyframes are recorded for the clip JSON."""

    def __init__(self, analysis: dict, src_w: int, src_h: int, out_w: int, out_h: int, forced_layout: str = "auto",
                 zoom_fn=None):
        self.a = analysis
        self.sw, self.sh, self.ow, self.oh = src_w, src_h, out_w, out_h
        self.forced = forced_layout if forced_layout in ("single", "split", "wide", "speaker") else "auto"
        self.zoom_fn = zoom_fn  # t_out -> zoom factor (1.3)
        self.shots = analysis["shots"]
        self.cur_shot = None
        self.sx = Spring(src_w / 2, float(R.get("ease_seconds", 0.6)))
        self.sy = Spring(src_h / 2, float(R.get("ease_seconds", 0.6)))
        self.sx2 = Spring(src_w / 2, float(R.get("ease_seconds", 0.6)))
        self.sy2 = Spring(src_h / 2, float(R.get("ease_seconds", 0.6)))
        self.last_t = None
        self.last_speaker = None
        self.switch_t = -10.0
        self.keyframes: list[list] = []
        self.next_key = 0.0
        self.bg_cache = None
        self.deadzone = float(R.get("dead_zone_frac", 0.10))
        self._tracks_by_id = {}

    # ---- helpers
    def shot_at(self, t: float) -> dict | None:
        for sh in self.shots:
            if sh["start"] - 1e-3 <= t < sh["end"] + 1e-3:
                return sh
        return self.shots[-1] if self.shots else None

    def layout_for(self, shot: dict) -> str:
        lay = shot["layout"]
        n = len(shot["tracks"])
        if self.forced != "auto":
            lay = self.forced
            if lay == "split" and n < 2:
                lay = "single" if n == 1 else "wide"
            if lay in ("single", "speaker") and n == 0:
                lay = "wide"
        if self.ow >= self.oh and lay in ("split", "wide"):
            lay = "wide"
        if lay == "speaker":
            lay = "single"
        if lay == "single" and n == 0:
            lay = "wide"
        return lay

    def speaker_track(self, shot: dict, t: float) -> dict:
        tracks = shot["tracks"]
        tid = None
        for t0, t1, sid, conf in shot.get("speaker", []):
            if t0 <= t < t1:
                tid = sid
                break
        if tid is None and shot.get("speaker"):
            tid = shot["speaker"][-1][2]
        for tr in tracks:
            if tr["id"] == tid:
                return tr
        return tracks[0]

    @staticmethod
    def face_pos_at(track: dict, t: float) -> tuple[float, float, float]:
        pts = track["pts"]
        if not pts:
            return track["cx"], track["cy"], track["h"]
        if t <= pts[0][0]:
            p = pts[0]
            return p[1], p[2], p[4]
        for i in range(1, len(pts)):
            if pts[i][0] >= t:
                p0, p1 = pts[i - 1], pts[i]
                f = (t - p0[0]) / max(1e-6, p1[0] - p0[0])
                return p0[1] + f * (p1[1] - p0[1]), p0[2] + f * (p1[2] - p0[2]), p0[4] + f * (p1[4] - p0[4])
        p = pts[-1]
        return p[1], p[2], p[4]

    def crop_size(self, aspect: float, zoom: float = 1.0) -> tuple[float, float]:
        """Largest source crop with the given aspect (w/h), divided by zoom."""
        if self.sw / self.sh > aspect:
            ch = self.sh
            cw = ch * aspect
        else:
            cw = self.sw
            ch = cw / aspect
        return cw / zoom, ch / zoom

    def target_for(self, track: dict, t: float, cw: float, ch: float, y_frac: float, spring_x: Spring, spring_y: Spring,
                   dt: float, reset: bool, fast: bool = False) -> tuple[float, float]:
        fx, fy, fh = self.face_pos_at(track, t)
        # steady target = shot median; follow only when the face leaves the dead zone
        mx, my = track["cx"], track["cy"]
        tx = mx if abs(fx - mx) < self.deadzone * cw else fx
        ty = my if abs(fy - my) < self.deadzone * ch else fy
        # crop centre so the face sits at y_frac of the crop
        cx = tx
        cy = ty - (y_frac - 0.5) * ch
        cx = min(max(cx, cw / 2), self.sw - cw / 2)
        cy = min(max(cy, ch / 2), self.sh - ch / 2)
        if reset:
            spring_x.reset(cx)
            spring_y.reset(cy)
            return cx, cy
        tau = float(R.get("switch_ease_seconds", 0.35)) if fast else None
        x = spring_x.step(cx, dt, tau)
        y = spring_y.step(cy, dt, tau)
        x = min(max(x, cw / 2), self.sw - cw / 2)
        y = min(max(y, ch / 2), self.sh - ch / 2)
        return x, y

    def crop(self, img: np.ndarray, cx: float, cy: float, cw: float, ch: float, w: int, h: int) -> np.ndarray:
        x0 = int(round(cx - cw / 2))
        y0 = int(round(cy - ch / 2))
        x0 = min(max(0, x0), max(0, self.sw - int(cw)))
        y0 = min(max(0, y0), max(0, self.sh - int(ch)))
        x1, y1 = min(self.sw, x0 + int(round(cw))), min(self.sh, y0 + int(round(ch)))
        part = img[y0:y1, x0:x1]
        if part.size == 0:
            return np.zeros((h, w, 3), dtype=np.uint8)
        return cv2.resize(part, (w, h), interpolation=cv2.INTER_AREA if part.shape[1] > w else cv2.INTER_LINEAR)

    def wide(self, img: np.ndarray, zoom: float = 1.0) -> np.ndarray:
        ow, oh = self.ow, self.oh
        # background: blurred, darkened, fills the frame
        small = cv2.resize(img, (ow // 6, oh // 6), interpolation=cv2.INTER_AREA) if False else None
        cw, ch = self.crop_size(ow / oh)
        bg_src = self.crop(img, self.sw / 2, self.sh / 2, cw, ch, ow // 8, oh // 8)
        k = int(R.get("bg_blur", 31)) // 4 * 2 + 1
        bg = cv2.GaussianBlur(bg_src, (k, k), 0)
        bg = (bg.astype(np.float32) * (1 - float(R.get("bg_dark", 0.45)))).astype(np.uint8)
        out = cv2.resize(bg, (ow, oh), interpolation=cv2.INTER_LINEAR)
        # foreground: full frame fitted to width (zoom crops a little from the middle)
        fw = ow
        fh = int(round(ow * self.sh / self.sw))
        if fh > oh:
            fh = oh
            fw = int(round(oh * self.sw / self.sh))
        if zoom > 1.0:
            zw, zh = self.sw / zoom, self.sh / zoom
            fg = self.crop(img, self.sw / 2, self.sh / 2, zw, zh, fw, fh)
        else:
            fg = cv2.resize(img, (fw, fh), interpolation=cv2.INTER_AREA)
        y0 = (oh - fh) // 2
        x0 = (ow - fw) // 2
        out[y0:y0 + fh, x0:x0 + fw] = fg
        return out

    # ---- main entry
    def frame(self, img: np.ndarray, t: float, t_out: float) -> np.ndarray:
        shot = self.shot_at(t)
        dt = 1 / 30 if self.last_t is None else max(1e-3, min(0.2, t_out - self.last_t))
        self.last_t = t_out
        new_shot = shot is not self.cur_shot
        self.cur_shot = shot
        zoom = float(self.zoom_fn(t_out)) if self.zoom_fn else 1.0
        if shot is None:
            out = self.wide(img, zoom)
            self._key(t_out, "wide", None)
            return out
        lay = self.layout_for(shot)
        if lay == "wide":
            out = self.wide(img, zoom)
            self._key(t_out, "wide", None)
            return out
        if lay == "split":
            tr = sorted(shot["tracks"][:2], key=lambda x: x["cx"])
            top, bottom = tr[0], tr[1]
            half_h = (self.oh - int(R.get("divider_px", 6))) // 2
            aspect = self.ow / half_h
            cw, ch = self.crop_size(aspect, zoom)
            yf = float(R.get("split_face_y_frac", 0.45))
            c1 = self.target_for(top, t, cw, ch, yf, self.sx, self.sy, dt, new_shot)
            c2 = self.target_for(bottom, t, cw, ch, yf, self.sx2, self.sy2, dt, new_shot)
            a = self.crop(img, c1[0], c1[1], cw, ch, self.ow, half_h)
            b = self.crop(img, c2[0], c2[1], cw, ch, self.ow, half_h)
            out = np.empty((self.oh, self.ow, 3), dtype=np.uint8)
            out[:] = hex_bgr(R.get("divider_color", "#0f1115"))
            out[:half_h] = a
            out[self.oh - half_h:] = b
            self._key(t_out, "split", [c1, c2, (cw, ch)])
            return out
        # single: follow the speaker
        track = self.speaker_track(shot, t)
        fast = False
        if self.last_speaker is not None and track["id"] != self.last_speaker and not new_shot:
            fast = True
            self.switch_t = t_out
        elif t_out - self.switch_t < float(R.get("switch_ease_seconds", 0.35)) * 2:
            fast = True
        self.last_speaker = track["id"]
        cw, ch = self.crop_size(self.ow / self.oh, zoom)
        cx, cy = self.target_for(track, t, cw, ch, float(R.get("face_y_frac", 0.42)), self.sx, self.sy, dt, new_shot, fast)
        out = self.crop(img, cx, cy, cw, ch, self.ow, self.oh)
        self._key(t_out, "single", [(cx, cy), (cw, ch), track["id"]])
        return out

    def _key(self, t_out: float, layout: str, data):
        if t_out >= self.next_key:
            self.keyframes.append([round(t_out, 2), layout, _rnd(data)])
            self.next_key = t_out + 0.5


def _rnd(x):
    if isinstance(x, (list, tuple)):
        return [_rnd(v) for v in x]
    if isinstance(x, float):
        return round(x, 1)
    return x


def summary(analysis: dict) -> list[dict]:
    return [{"start": s["start"], "end": s["end"], "layout": s["layout"], "faces": len(s["tracks"]),
             "speakers": [[a, b, i] for a, b, i, c in s.get("speaker", [])],
             "confidence": (s.get("speaker") or [[0, 0, 0, 0]])[0][3]} for s in analysis["shots"]]
