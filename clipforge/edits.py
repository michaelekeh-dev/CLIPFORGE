"""Editor changes stored on a clip: new range, deleted word ranges, caption text fixes, per-shot layouts."""
from __future__ import annotations
from .timeline import Timeline


def normalise(edits: dict | None, orig_start: float, orig_end: float) -> dict:
    e = dict(edits or {})
    try:
        s = float(e.get("start", orig_start))
        en = float(e.get("end", orig_end))
    except (TypeError, ValueError):
        s, en = orig_start, orig_end
    if en - s < 2.0:
        s, en = orig_start, orig_end
    deleted = []
    for d in e.get("deleted") or []:
        try:
            a, b = float(d[0]), float(d[1])
            if b > a:
                deleted.append([round(a, 3), round(b, 3)])
        except (TypeError, ValueError, IndexError):
            continue
    fixes = {str(k): str(v)[:60] for k, v in (e.get("text_fixes") or {}).items()}
    shots = {str(k): v for k, v in (e.get("shot_layouts") or {}).items() if v in ("auto", "single", "split", "wide")}
    return {"start": round(s, 3), "end": round(en, 3), "deleted": sorted(deleted), "text_fixes": fixes, "shot_layouts": shots}


def apply_text_fixes(words: list[dict], fixes: dict) -> list[dict]:
    """Words carry their transcript index in 'i'; fixed words get the new text (empty = drop from captions)."""
    if not fixes:
        return words
    out = []
    for w in words:
        k = str(w.get("i", ""))
        if k in fixes:
            t = fixes[k].strip()
            if not t:
                continue
            out.append({**w, "w": t})
        else:
            out.append(w)
    return out


def timeline_for(edits: dict) -> Timeline:
    tl = Timeline.single(edits["start"], edits["end"])
    if edits["deleted"]:
        tl = tl.remove([(a, b) for a, b in edits["deleted"]])
    return tl


def word_ranges(words: list[dict], indices: list[int], margin: float = 0.02) -> list[list[float]]:
    """Source ranges that remove the given transcript words without touching their neighbours."""
    by_i = {w["i"]: w for w in words}
    out = []
    for i in sorted(set(indices)):
        w = by_i.get(i)
        if not w:
            continue
        s = w["s"] - margin
        e = w["e"] + margin
        prev, nxt = by_i.get(i - 1), by_i.get(i + 1)
        if prev:
            s = max(s, prev["e"] + 0.01)
        if nxt:
            e = min(e, nxt["s"] - 0.01)
        if e > s:
            out.append([round(s, 3), round(e, 3)])
    return out
