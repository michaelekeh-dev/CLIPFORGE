"""Render one clip: decode the kept pieces, frame them for the output ratio, pipe into ffmpeg, add audio."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
import numpy as np
import cv2
from . import media, audio
from .config import cfg, output_size
from .timeline import Timeline


class Framer:
    """Turns a source frame at source time t into an output frame. 1.0: centre crop."""

    def __init__(self, src_w: int, src_h: int, out_w: int, out_h: int, **kw):
        self.sw, self.sh, self.ow, self.oh = src_w, src_h, out_w, out_h
        self.crop_w, self.crop_h = fit_crop(src_w, src_h, out_w, out_h)

    def frame(self, img: np.ndarray, t: float, t_out: float) -> np.ndarray:
        x = (self.sw - self.crop_w) // 2
        y = (self.sh - self.crop_h) // 2
        crop = img[y:y + self.crop_h, x:x + self.crop_w]
        return cv2.resize(crop, (self.ow, self.oh), interpolation=cv2.INTER_AREA if self.crop_w > self.ow else cv2.INTER_LINEAR)


def fit_crop(sw: int, sh: int, ow: int, oh: int) -> tuple[int, int]:
    """Largest source crop with the output aspect ratio."""
    if sw / sh > ow / oh:
        ch = sh
        cw = int(round(sh * ow / oh))
    else:
        cw = sw
        ch = int(round(sw * oh / ow))
    return min(cw, sw) // 2 * 2, min(ch, sh) // 2 * 2


def decode_frames(source: Path, start: float, end: float, fps: float, w: int, h: int):
    """Yield BGR frames from source between start and end at the given fps (frame-accurate via ffmpeg)."""
    dur = max(0.0, end - start)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", str(source),
           "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-an", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 7)
    size = w * h * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.wait()


def render_clip(source: Path, tl: Timeline, out_path: Path, workdir: Path, framer: Framer | None = None,
                ratio: str = "9:16", subtitles: Path | None = None, overlays=None, progress=None,
                video_filters: list[str] | None = None) -> dict:
    """Render the clip. `overlays(frame, t_src, t_out)` may draw on each output frame."""
    workdir.mkdir(parents=True, exist_ok=True)
    info = media.probe(source)
    ow, oh = output_size(ratio)
    fps = float(cfg.get("render.fps", 30))
    if info["fps"] and info["fps"] < fps:
        fps = round(info["fps"], 3)
    sw, sh = info["width"], info["height"]
    framer = framer or Framer(sw, sh, ow, oh)

    if progress:
        progress("Rendering: audio", 0)
    wav = workdir / "clip_audio.wav"
    audio.build_clip_audio(source, tl, wav, workdir)
    measured = media.measure_loudness(wav, float(cfg.get("render.loudness", -14)), float(cfg.get("render.true_peak", -1.5)))
    af = media.loudnorm_filter(measured, float(cfg.get("render.loudness", -14)), float(cfg.get("render.true_peak", -1.5)))

    vf = list(video_filters or [])
    if subtitles:
        vf.append(f"ass={_esc(subtitles)}")
    cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x{oh}", "-r", f"{fps}", "-i", "pipe:0",
           "-i", str(wav),
           "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", cfg.get("render.preset", "veryfast"), "-crf", str(cfg.get("render.crf", 20)),
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", cfg.get("render.audio_bitrate", "160k"), "-ar", "48000", "-af", af,
           "-shortest", str(out_path)]
    if vf:
        cmd[cmd.index("-map"):cmd.index("-map")] = ["-vf", ",".join(vf)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 7)
    total = int(tl.duration * fps) or 1
    n = 0
    t_out = 0.0
    try:
        for (s, e) in tl.pieces:
            for img in decode_frames(source, s, e, fps, sw, sh):
                t_src = s + (t_out - _piece_offset(tl, s))
                out = framer.frame(img, t_src, t_out)
                if overlays:
                    out = overlays(out, t_src, t_out)
                enc.stdin.write(np.ascontiguousarray(out).tobytes())
                n += 1
                t_out = n / fps
                if progress and n % 30 == 0:
                    progress("Rendering: video", 100 * n / total)
    except BrokenPipeError:
        pass
    finally:
        try:
            enc.stdin.close()
        except Exception:
            pass
        err = enc.stderr.read().decode(errors="ignore")
        enc.wait()
    if enc.returncode != 0 or not out_path.exists():
        raise RuntimeError("ffmpeg failed: " + err[-800:])
    return {"frames": n, "fps": fps, "width": ow, "height": oh, "duration": round(tl.duration, 3), "loudness": measured}


def _piece_offset(tl: Timeline, s: float) -> float:
    acc = 0.0
    for a, b in tl.pieces:
        if a == s:
            return acc
        acc += b - a
    return acc


def _esc(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
