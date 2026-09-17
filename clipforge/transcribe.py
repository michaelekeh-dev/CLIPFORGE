"""Speech to text with word timestamps. Cached per audio file.

Output format (transcript.json):
{
  "backend": "parakeet" | "whisper",
  "language": "en",
  "words": [{"w": "Hello", "s": 0.24, "e": 0.51}, ...],
  "segments": [{"s": 0.24, "e": 3.1, "text": "Hello there.", "wi": 0, "wj": 2}, ...]
}
"""
from __future__ import annotations
import hashlib
import json
import re
import tarfile
import time
import urllib.request
import wave
from pathlib import Path
import numpy as np
from . import media
from .config import MODELS, cfg, device

SENT_END = re.compile(r"[.!?]+[\"')\]]*$")


def _file_key(path: Path) -> str:
    h = hashlib.sha1()
    st = path.stat()
    h.update(f"{path.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def choose_backend() -> str:
    want = cfg.get("transcribe.backend", "auto")
    if want != "auto":
        return want
    if device() == "cuda":
        return "whisper"
    if parakeet_dir(download=False):
        return "parakeet"
    try:
        import sherpa_onnx  # noqa: F401
        return "parakeet"
    except Exception:
        return "whisper"


def transcribe(source: Path, workdir: Path, progress=None, start: float | None = None, end: float | None = None) -> dict:
    """Transcribe `source` (any media). Cached in workdir/transcript.json."""
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "transcript.json"
    wav = workdir / "audio16k.wav"
    backend = choose_backend()
    key = f"{_file_key(source)}:{backend}:{start}:{end}"
    if out.exists():
        try:
            data = json.loads(out.read_text())
            if data.get("key") == key and data.get("words"):
                return data
        except Exception:
            pass
    if progress:
        progress("Transcribing", 22)
    if not wav.exists():
        media.extract_audio(source, wav, sr=16000, mono=True)
    if backend == "whisper":
        data = _whisper(wav, progress)
    else:
        try:
            data = _parakeet(wav, progress)
        except Exception as e:  # noqa: BLE001
            if cfg.get("transcribe.backend", "auto") != "auto":
                raise
            data = _whisper(wav, progress, note=f"Parakeet failed ({e}); used whisper")
    if start is not None or end is not None:
        s0 = start or 0.0
        e0 = end or 1e12
        data["words"] = [w for w in data["words"] if w["s"] >= s0 and w["e"] <= e0]
    data["segments"] = make_segments(data["words"])
    data["key"] = key
    data["text"] = " ".join(w["w"] for w in data["words"])
    out.write_text(json.dumps(data, ensure_ascii=False))
    return data


# ----------------------------------------------------------------------------- whisper
def _whisper(wav: Path, progress=None, note: str = "") -> dict:
    from faster_whisper import WhisperModel
    dev = device()
    name = cfg.get("transcribe.whisper_model_gpu" if dev == "cuda" else "transcribe.whisper_model_cpu", "small")
    model = WhisperModel(name, device=dev, compute_type="float16" if dev == "cuda" else "int8",
                         download_root=str(MODELS / "whisper"))
    lang = cfg.get("transcribe.language") or None
    segments, info = model.transcribe(str(wav), word_timestamps=True, language=lang, vad_filter=True,
                                      beam_size=5, condition_on_previous_text=False)
    total = _wav_duration(wav) or 1
    words = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append({"w": t, "s": round(w.start, 3), "e": round(max(w.end, w.start + 0.05), 3), "p": round(w.probability, 2)})
        if progress:
            progress("Transcribing", 22 + 18 * min(1.0, seg.end / total))
    return {"backend": f"whisper-{name}", "language": getattr(info, "language", lang) or "en", "words": words, "note": note}


# ----------------------------------------------------------------------------- parakeet (sherpa-onnx)
def parakeet_dir(download: bool = True) -> Path | None:
    name = cfg.get("transcribe.parakeet_model")
    d = MODELS / name
    if (d / "encoder.int8.onnx").exists() or (d / "encoder.onnx").exists():
        return d
    if not download:
        return None
    url = cfg.get("transcribe.parakeet_url")
    MODELS.mkdir(parents=True, exist_ok=True)
    tmp = MODELS / (name + ".tar.bz2")
    urllib.request.urlretrieve(url, tmp)
    with tarfile.open(tmp, "r:bz2") as tf:
        tf.extractall(MODELS)
    tmp.unlink(missing_ok=True)
    return d if (d / "tokens.txt").exists() else None


_parakeet_model = None


def _load_parakeet():
    global _parakeet_model
    if _parakeet_model is not None:
        return _parakeet_model
    import sherpa_onnx
    d = parakeet_dir(download=True)
    if d is None:
        raise RuntimeError("Parakeet model missing")
    enc = d / "encoder.int8.onnx" if (d / "encoder.int8.onnx").exists() else d / "encoder.onnx"
    dec = d / "decoder.int8.onnx" if (d / "decoder.int8.onnx").exists() else d / "decoder.onnx"
    joi = d / "joiner.int8.onnx" if (d / "joiner.int8.onnx").exists() else d / "joiner.onnx"
    import os
    _parakeet_model = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(enc), decoder=str(dec), joiner=str(joi), tokens=str(d / "tokens.txt"),
        num_threads=max(1, min(8, os.cpu_count() or 2)), model_type="nemo_transducer",
        provider="cuda" if device() == "cuda" else "cpu")
    return _parakeet_model


def _read_wav(wav: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return data, sr


def _wav_duration(wav: Path) -> float:
    try:
        with wave.open(str(wav)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def _chunks(samples: np.ndarray, sr: int, target: float = 30.0) -> list[tuple[int, int]]:
    """Split on the quietest point near each 30s boundary so words are not cut."""
    n = len(samples)
    tgt = int(target * sr)
    win = int(0.02 * sr)
    out = []
    pos = 0
    while n - pos > tgt * 1.3:
        lo = pos + int(tgt * 0.8)
        hi = min(n - 1, pos + int(tgt * 1.2))
        seg = samples[lo:hi]
        m = len(seg) // win * win
        if m < win:
            cut = pos + tgt
        else:
            energy = (seg[:m].reshape(-1, win) ** 2).mean(axis=1)
            cut = lo + int(np.argmin(energy)) * win + win // 2
        out.append((pos, cut))
        pos = cut
    out.append((pos, n))
    return out


def _parakeet(wav: Path, progress=None) -> dict:
    rec = _load_parakeet()
    samples, sr = _read_wav(wav)
    chunks = _chunks(samples, sr, float(cfg.get("transcribe.chunk_seconds", 30)))
    words: list[dict] = []
    t0 = time.time()
    for i, (a, b) in enumerate(chunks):
        s = rec.create_stream()
        s.accept_waveform(sr, samples[a:b])
        rec.decode_stream(s)
        r = s.result
        off = a / sr
        toks = list(r.tokens)
        ts = list(r.timestamps)
        durs = list(getattr(r, "durations", []) or [])
        for k, tok in enumerate(toks):
            start = off + ts[k]
            end = start + (durs[k] if k < len(durs) and durs[k] > 0 else 0.08)
            if k + 1 < len(toks):
                end = min(end, off + ts[k + 1]) if durs and k < len(durs) and durs[k] > 0 else max(end, off + ts[k + 1] - 0.01)
            new_word = tok.startswith(" ") or tok.startswith("▁") or not words
            text = tok.replace("▁", " ")
            if new_word and text.strip():
                words.append({"w": text.strip(), "s": round(start, 3), "e": round(max(end, start + 0.05), 3)})
            elif words and text.strip():
                words[-1]["w"] += text.strip()
                words[-1]["e"] = round(max(words[-1]["e"], end), 3)
        if progress:
            progress("Transcribing", 22 + 18 * (i + 1) / len(chunks))
    # words shouldn't overlap the next word
    for i in range(len(words) - 1):
        if words[i]["e"] > words[i + 1]["s"]:
            words[i]["e"] = round(max(words[i]["s"] + 0.05, words[i + 1]["s"]), 3)
    return {"backend": "parakeet", "language": "en", "words": words, "seconds": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------- segments
def make_segments(words: list[dict], max_words: int = 28, gap: float = 0.8) -> list[dict]:
    segs = []
    i = 0
    n = len(words)
    while i < n:
        j = i
        while j < n:
            w = words[j]
            done = bool(SENT_END.search(w["w"])) or (j - i + 1 >= max_words)
            if j + 1 < n and words[j + 1]["s"] - w["e"] > gap:
                done = True
            if done:
                break
            j += 1
        j = min(j, n - 1)
        segs.append({"s": words[i]["s"], "e": words[j]["e"], "text": " ".join(x["w"] for x in words[i:j + 1]), "wi": i, "wj": j})
        i = j + 1
    return segs


def fmt_ts(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:05.2f}"
