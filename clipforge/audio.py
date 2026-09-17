"""Clip audio: cut the kept pieces from the source, join with short crossfades, keep it click-free."""
from __future__ import annotations
import wave
from pathlib import Path
import numpy as np
from . import media
from .timeline import Timeline

SR = 48000
XFADE = 0.012  # seconds of crossfade at every join


def _read(wav: Path) -> np.ndarray:
    with wave.open(str(wav)) as w:
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return data.reshape(-1, ch)


def write_wav(path: Path, data: np.ndarray, sr: int = SR):
    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(data.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def build_clip_audio(source: Path, tl: Timeline, out: Path, workdir: Path) -> Path:
    """Extract source audio for the clip range once, then assemble the kept pieces with crossfades."""
    base = tl.start
    span = tl.end - tl.start
    raw = workdir / "clip_audio_raw.wav"
    media.extract_audio(source, raw, sr=SR, mono=False, start=base, dur=span + 0.5)
    data = _read(raw)
    n = len(data)
    ch = data.shape[1]
    xf = int(XFADE * SR)
    pieces = []
    for s, e in tl.pieces:
        a = int(round((s - base) * SR))
        b = int(round((e - base) * SR))
        a, b = max(0, min(a, n)), max(0, min(b, n))
        if b - a < 2:
            continue
        pieces.append(data[a:b].copy())
    if not pieces:
        pieces = [np.zeros((SR, ch), dtype=np.float32)]
    result = pieces[0]
    for p in pieces[1:]:
        k = min(xf, len(result), len(p))
        if k > 8:
            ramp = np.linspace(0, 1, k, dtype=np.float32)[:, None]
            tail = result[-k:] * (1 - ramp) + p[:k] * ramp
            result = np.concatenate([result[:-k], tail, p[k:]])
        else:
            result = np.concatenate([result, p])
    # tiny fade in/out so there are never clicks at the clip edges
    k = min(int(0.01 * SR), len(result))
    if k > 8:
        ramp = np.linspace(0, 1, k, dtype=np.float32)[:, None]
        result[:k] *= ramp
        result[-k:] *= ramp[::-1]
    write_wav(out, result)
    raw.unlink(missing_ok=True)
    return out
