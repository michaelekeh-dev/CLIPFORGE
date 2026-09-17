"""Filler words, false starts and long silences -> list of source-time cuts."""
from __future__ import annotations
import re
import numpy as np
from .config import cfg

F = cfg.get("filler")


def _norm(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())


def find_cuts(words: list[dict], level: str, audio_rms: dict | None = None, start: float | None = None,
              end: float | None = None) -> list[dict]:
    """Returns [{"s", "e", "why"}] in source time. `words` are the clip's words (source time)."""
    if level not in ("light", "aggressive") or not words:
        return []
    margin = float(F.get("margin", 0.05))
    fillers = {_norm(x) for x in F.get("fillers", [])}
    aggr = [x.lower() for x in F.get("aggressive_fillers", [])]
    cuts: list[dict] = []
    n = len(words)
    removed = set()

    def bounded(s, e, i_prev, i_next):
        """Clamp a cut so it never touches a neighbouring kept word."""
        lo = words[i_prev]["e"] + margin if i_prev is not None else (start if start is not None else s)
        hi = words[i_next]["s"] - margin if i_next is not None else (end if end is not None else e)
        s, e = max(s, lo), min(e, hi)
        return (s, e) if e - s > 0.03 else None

    # 1. filler words
    for i, w in enumerate(words):
        t = _norm(w["w"])
        hit = t in fillers
        if not hit and level == "aggressive":
            two = (t + " " + _norm(words[i + 1]["w"])) if i + 1 < n else ""
            if two in aggr:
                # two-word filler ("you know"): only when it is followed by a pause or punctuation
                nxt = words[i + 1]
                if re.search(r"[,.!?]$", nxt["w"]) or (i + 2 < n and words[i + 2]["s"] - nxt["e"] > 0.25) or i + 2 >= n:
                    r = bounded(w["s"] - 0.02, nxt["e"] + 0.02, i - 1 if i else None, i + 2 if i + 2 < n else None)
                    if r:
                        cuts.append({"s": r[0], "e": r[1], "why": f"filler: {w['w']} {nxt['w']}"})
                        removed.update({i, i + 1})
                continue
            if t in aggr and (re.search(r"[,.!?]$", w["w"]) or (i + 1 < n and words[i + 1]["s"] - w["e"] > 0.25)):
                hit = True
        if hit and i not in removed:
            r = bounded(w["s"] - 0.02, w["e"] + 0.02, i - 1 if i else None, i + 1 if i + 1 < n else None)
            if r:
                cuts.append({"s": r[0], "e": r[1], "why": f"filler: {w['w']}"})
                removed.add(i)

    # 2. false starts: "I I think", "we were, we were going" -> drop the first copy
    i = 0
    while i < n - 1:
        if i in removed:
            i += 1
            continue
        a = _norm(words[i]["w"])
        if a and a == _norm(words[i + 1]["w"]) and i + 1 not in removed and words[i + 1]["s"] - words[i]["e"] < 0.6:
            r = bounded(words[i]["s"] - 0.02, words[i]["e"] + 0.02, i - 1 if i else None, i + 1)
            if r:
                cuts.append({"s": r[0], "e": r[1], "why": f"false start: {words[i]['w']}"})
                removed.add(i)
            i += 2
            continue
        if level == "aggressive" and i + 3 < n:
            p1 = (a, _norm(words[i + 1]["w"]))
            p2 = (_norm(words[i + 2]["w"]), _norm(words[i + 3]["w"]))
            if p1 == p2 and all(p1) and words[i + 2]["s"] - words[i + 1]["e"] < 0.6 and not ({i, i + 1, i + 2, i + 3} & removed):
                r = bounded(words[i]["s"] - 0.02, words[i + 1]["e"] + 0.02, i - 1 if i else None, i + 2)
                if r:
                    cuts.append({"s": r[0], "e": r[1], "why": f"false start: {words[i]['w']} {words[i + 1]['w']}"})
                    removed.update({i, i + 1})
                i += 4
                continue
        i += 1

    # 3. silences between words
    gap = float(F.get("silence_gap", 0.6))
    keep = float(F.get("keep_pause", {}).get(level, 0.3))
    for i in range(n - 1):
        a, b = words[i], words[i + 1]
        g = b["s"] - a["e"]
        if g <= gap:
            continue
        if audio_rms and not _quiet(audio_rms, a["e"] + keep / 2, b["s"] - keep / 2):
            continue  # laughter, music, a reaction: keep it
        s, e = a["e"] + keep / 2, b["s"] - keep / 2
        if e - s > 0.1:
            cuts.append({"s": round(s, 3), "e": round(e, 3), "why": f"silence {g:.1f}s"})
    cuts.sort(key=lambda c: c["s"])
    # merge overlaps
    merged: list[dict] = []
    for c in cuts:
        if merged and c["s"] <= merged[-1]["e"] + 0.01:
            merged[-1]["e"] = max(merged[-1]["e"], c["e"])
            merged[-1]["why"] += "; " + c["why"]
        else:
            merged.append(dict(c))
    return merged


def _quiet(audio_rms: dict, s: float, e: float) -> bool:
    hop, st, rms = audio_rms["hop"], audio_rms["start"], audio_rms["rms"]
    i0, i1 = int((s - st) / hop), int((e - st) / hop)
    seg = rms[max(0, i0):max(0, i1) + 1]
    if not seg:
        return True
    return float(np.percentile(seg, 80)) < audio_rms["thresh"] * 1.2
