"""ffmpeg / ffprobe helpers used everywhere."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path


def run(cmd: list[str], check: bool = True, capture: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=check, capture_output=capture, text=True, timeout=timeout)


def probe(path: str | Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate,avg_frame_rate,nb_frames,sample_rate,channels",
               "-of", "json", path]).stdout
    d = json.loads(out)
    info = {"duration": float(d.get("format", {}).get("duration") or 0), "width": 0, "height": 0, "fps": 30.0, "has_audio": False}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and not info["width"]:
            info["width"] = int(s.get("width") or 0)
            info["height"] = int(s.get("height") or 0)
            fr = s.get("avg_frame_rate") or s.get("r_frame_rate") or "30/1"
            try:
                n, m = fr.split("/")
                info["fps"] = float(n) / float(m) if float(m) else 30.0
            except Exception:
                info["fps"] = 30.0
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def thumbnail(src: str | Path, out: str | Path, t: float = 1.0, width: int = 640):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "3", out])


def extract_audio(src: str | Path, out: str | Path, sr: int = 16000, mono: bool = True, start: float | None = None, dur: float | None = None):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-i", src, "-vn", "-ar", str(sr)]
    if mono:
        cmd += ["-ac", "1"]
    cmd += ["-c:a", "pcm_s16le", out]
    run(cmd)


def measure_loudness(wav: str | Path, target: float = -14, tp: float = -1.5) -> dict:
    """First loudnorm pass: returns measured values for the second pass."""
    r = run(["ffmpeg", "-v", "info", "-hide_banner", "-nostats", "-i", wav, "-af",
             f"loudnorm=I={target}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"], check=False)
    txt = r.stderr
    i = txt.rfind("{")
    if i < 0:
        return {}
    try:
        return json.loads(txt[i:txt.rfind("}") + 1])
    except json.JSONDecodeError:
        return {}


def loudnorm_filter(measured: dict, target: float = -14, tp: float = -1.5) -> str:
    base = f"loudnorm=I={target}:TP={tp}:LRA=11"
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if all(k in measured for k in keys):
        try:
            vals = {k: float(measured[k]) for k in keys}
            if all(abs(v) < 1e6 for v in vals.values()):
                return (base + f":measured_I={vals['input_i']}:measured_TP={vals['input_tp']}:measured_LRA={vals['input_lra']}"
                        f":measured_thresh={vals['input_thresh']}:offset={vals['target_offset']}:linear=true")
        except (TypeError, ValueError):
            pass
    return base
