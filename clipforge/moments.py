"""Find the best moments. Claude picks when a key is set; a keyword/hook heuristic otherwise."""
from __future__ import annotations
import re
from .config import cfg
from . import llm
from .transcribe import fmt_ts, SENT_END

MOMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "score": {"type": "integer"},
                    "reasons": {
                        "type": "object",
                        "properties": {
                            "hook": {"type": "string"}, "payoff": {"type": "string"}, "emotion": {"type": "string"},
                            "standalone": {"type": "string"}, "topic_match": {"type": "string"},
                        },
                        "required": ["hook", "payoff", "emotion", "standalone", "topic_match"],
                        "additionalProperties": False,
                    },
                    "topic": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "key_words": {"type": "array", "items": {"type": "string"}},
                    "emojis": {"type": "array", "items": {"type": "object", "properties": {
                        "word": {"type": "string"}, "emoji": {"type": "string"}},
                        "required": ["word", "emoji"], "additionalProperties": False}},
                    "hook": {"type": "string"},
                    "zooms": {"type": "array", "items": {"type": "number"}},
                    "broll": {"type": "array", "items": {"type": "object", "properties": {
                        "t": {"type": "number"}, "query": {"type": "string"}, "word": {"type": "string"}},
                        "required": ["t", "query", "word"], "additionalProperties": False}},
                },
                "required": ["start", "end", "score", "reasons", "topic", "title", "description", "hashtags", "key_words",
                             "emojis", "hook", "zooms", "broll"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

SYSTEM = """You are a senior short-form video editor. You pick the moments from a long talking video that would work
best as standalone vertical Shorts for a channel about theories, history and Christian faith content.
Rules for every moment:
- start on a strong first sentence (a hook, a question, a bold claim, a story opening), never mid-sentence
- end on a natural ending (the payoff, the punchline, the conclusion), never mid-sentence
- it must make complete sense to someone who has not seen the rest of the video
- keep it within the requested length; use the transcript timestamps exactly (seconds)
- never invent or change what was said; titles must be honest and frame theories as theories
Return JSON only."""


def length_range(preset: str) -> tuple[float, float]:
    d = cfg.get("moments.lengths", {}).get(preset) or cfg.get("moments.lengths.auto")
    return float(d["min"]), float(d["max"])


def chunk_transcript(tr: dict, chunk_seconds: float) -> list[dict]:
    chunks, cur, cur_start = [], [], None
    for seg in tr["segments"]:
        if cur_start is None:
            cur_start = seg["s"]
        cur.append(seg)
        if seg["e"] - cur_start >= chunk_seconds:
            chunks.append({"start": cur_start, "end": seg["e"], "segments": cur})
            cur, cur_start = [], None
    if cur:
        chunks.append({"start": cur_start, "end": cur[-1]["e"], "segments": cur})
    return chunks


def chunk_text(chunk: dict) -> str:
    return "\n".join(f"[{seg['s']:.2f}] {seg['text']}" for seg in chunk["segments"])


def pick_moments(tr: dict, n: int, length: str, keywords: list[str], progress=None) -> tuple[list[dict], str]:
    """Returns (moments, method). Each moment has start/end snapped to words."""
    lo, hi = length_range(length)
    words = tr["words"]
    cands: list[dict] = []
    method = "claude"
    if llm.mode() == "live":
        chunks = chunk_transcript(tr, float(cfg.get("llm.chunk_seconds", 1200)))
        per = max(int(cfg.get("llm.candidates_per_chunk", 8)), n)
        for i, ch in enumerate(chunks):
            if progress:
                progress("Finding best moments", 42 + 12 * i / max(1, len(chunks)))
            prompt = (f"Video part {i + 1} of {len(chunks)} ({fmt_ts(ch['start'])} to {fmt_ts(ch['end'])}).\n"
                      f"Pick up to {per} of the best moments, each between {lo:.0f} and {hi:.0f} seconds long.\n"
                      f"Topic keywords the channel cares about (bonus, not a must): {', '.join(keywords) or 'none'}.\n"
                      "For each moment give: start and end in seconds (use the [timestamps]; end = the timestamp of the "
                      "sentence after the last one you keep, or the moment the last sentence ends), score 0-100 for "
                      "virality, one short sentence for each reason (hook, payoff, emotion, standalone, topic_match), "
                      "topic (3 words max), an honest catchy title (max 70 characters), a 1-2 sentence description, "
                      "5-8 hashtags, key_words: 1-3 words spoken in the clip worth highlighting in the captions, and emojis: "
                      "up to 3 pairs of a spoken word plus one fitting emoji (spread out, none is fine), "
                      "hook: a title card shown at the top of the clip, 6-12 words, honest, ideally a question or a bold "
                      "claim the clip actually answers (like 'What would you change if you could go back in time?'), "
                      "zooms: 1-3 timestamps in seconds of punchlines or reveals worth a punch-in zoom, "
                      "broll: up to 2 moments where something visual is mentioned (a place, an object, an animal): "
                      "t = the timestamp in seconds, word = the spoken word, query = 2-3 word stock footage search.\n\n"
                      f"TRANSCRIPT:\n{chunk_text(ch)}")
            try:
                out = llm.ask_json(prompt, system=SYSTEM, model=cfg.get("llm.pick_model"), schema=MOMENT_SCHEMA)
                for m in out.get("moments", []):
                    cands.append(m)
            except llm.LLMError as e:
                method = f"heuristic (Claude failed: {e})"
                cands = []
                break
    else:
        method = "heuristic (no ANTHROPIC_API_KEY)"
    if not cands:
        cands = heuristic_pick(tr, n * 3, lo, hi, keywords)
    snapped = []
    for m in cands:
        s = snap(m, words, lo, hi)
        if s:
            snapped.append(s)
    return dedupe(snapped, n), method


def snap(m: dict, words: list[dict], lo: float, hi: float) -> dict | None:
    """Move start/end onto word boundaries, keep it inside lo..hi, add padding."""
    if not words:
        return None
    try:
        start, end = float(m["start"]), float(m["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    # first word starting at/after start (tolerate 0.3s early)
    i = next((k for k, w in enumerate(words) if w["s"] >= start - 0.3), None)
    if i is None:
        return None
    # the tolerance may have caught the last word of the previous sentence: skip it
    while i + 1 < len(words) and SENT_END.search(words[i]["w"]) and words[i + 1]["s"] - start < 0.6:
        i += 1
    # last word ending at/before end (tolerate 0.3s late)
    j = max((k for k, w in enumerate(words) if w["e"] <= end + 0.3), default=None)
    if j is None or j < i:
        return None
    # prefer a sentence start: back up to previous sentence end within 2.5s
    k = i
    while k > 0 and not SENT_END.search(words[k - 1]["w"]) and words[i]["s"] - words[k - 1]["s"] < 2.5:
        k -= 1
    i = k
    # extend end to a sentence end within 3s if not already
    k = j
    while k + 1 < len(words) and not SENT_END.search(words[k]["w"]) and words[k + 1]["e"] - words[j]["e"] < 3.0:
        k += 1
    if SENT_END.search(words[k]["w"]):
        j = k
    # trim to max length at a sentence end, else at a word
    while words[j]["e"] - words[i]["s"] > hi and j > i:
        k = j - 1
        while k > i and not SENT_END.search(words[k]["w"]):
            k -= 1
        j = k if k > i and words[k]["e"] - words[i]["s"] >= lo else j - 1
    if words[j]["e"] - words[i]["s"] < max(3.0, lo * 0.6):
        return None
    pb, pa = float(cfg.get("moments.pad_before", 0.15)), float(cfg.get("moments.pad_after", 0.35))
    out = dict(m)
    out["start"] = round(max(0.0, words[i]["s"] - pb), 3)
    out["end"] = round(words[j]["e"] + pa, 3)
    out["wi"], out["wj"] = i, j
    out["score"] = int(max(0, min(100, int(m.get("score", 50)))))
    out.setdefault("reasons", {})
    out.setdefault("topic", "")
    out.setdefault("title", " ".join(w["w"] for w in words[i:i + 8]))
    out.setdefault("description", "")
    out.setdefault("hashtags", [])
    out.setdefault("key_words", [])
    out.setdefault("emojis", [])
    out.setdefault("hook", "")
    out.setdefault("zooms", [])
    out.setdefault("broll", [])
    return out


def dedupe(ms: list[dict], n: int) -> list[dict]:
    ms = sorted(ms, key=lambda m: -m["score"])
    keep: list[dict] = []
    for m in ms:
        if any(overlap(m, k) > 0.25 for k in keep):
            continue
        keep.append(m)
        if len(keep) >= n:
            break
    return keep


def overlap(a: dict, b: dict) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(0.1, min(a["end"] - a["start"], b["end"] - b["start"]))


# ----------------------------------------------------------------------------- heuristic fallback
HOOK_WORDS = {"imagine", "secret", "truth", "never", "nobody", "actually", "crazy", "insane", "why", "what", "how", "did",
              "you", "know", "believe", "wait", "listen", "here's", "this", "story", "real", "true", "hidden", "proof",
              "think", "question", "first", "biggest", "worst", "best"}
WEAK_START = {"and", "but", "so", "because", "or", "which", "that", "then", "also", "yeah", "um", "uh", "like"}
EMOTION = {"love", "hate", "afraid", "scared", "amazing", "terrible", "beautiful", "wow", "shocked", "cry", "laugh",
           "angry", "happy", "sad", "fear", "hope", "miracle", "wild", "funny", "dead", "death", "blood", "war"}


def heuristic_pick(tr: dict, n: int, lo: float, hi: float, keywords: list[str]) -> list[dict]:
    """Score every sentence window inside lo..hi, then greedily keep the best non-overlapping ones."""
    segs = tr["segments"]
    kws = {k.lower() for k in keywords}
    sweet = min(hi, max(lo, 45.0))  # length that usually reads best
    cands = []
    for i in range(len(segs)):
        j = i
        while j + 1 < len(segs) and segs[j]["e"] - segs[i]["s"] < lo:
            j += 1
        while j < len(segs):
            dur = segs[j]["e"] - segs[i]["s"]
            if dur > hi:
                break
            if dur >= lo:
                text = " ".join(s["text"] for s in segs[i:j + 1])
                toks = re.findall(r"[a-zA-Z']+", text.lower())
                if not toks:
                    break
                first = re.findall(r"[a-zA-Z']+", segs[i]["text"].lower())[:6]
                kw_hits = sum(1 for t in toks if t in kws)
                kw_rate = kw_hits / max(dur, 1) * 30
                hook = sum(1 for t in first if t in HOOK_WORDS)
                emo = sum(1 for t in toks if t in EMOTION) / max(dur, 1) * 30
                qmark = segs[i]["text"].count("?")
                weak = 1 if first and first[0] in WEAK_START else 0
                density = len(toks) / max(dur, 1)
                score = 40 + 10 * min(kw_rate, 4) + 8 * min(hook, 3) + 4 * min(emo, 3) + 6 * min(qmark, 1) - 15 * weak
                score += 5 if 2.0 <= density <= 3.5 else 0
                score -= 6 if not SENT_END.search(segs[j]["text"]) else 0
                score -= abs(dur - sweet) / 6
                cands.append({
                    "start": segs[i]["s"], "end": segs[j]["e"], "score": int(max(1, min(99, round(score)))),
                    "reasons": {
                        "hook": "Starts on a question or hook word" if (hook or qmark) else "Plain opening",
                        "payoff": "Ends on a full sentence" if SENT_END.search(segs[j]["text"]) else "Ending is soft",
                        "emotion": "Emotional words" if emo >= 1 else "Calm delivery",
                        "standalone": "Complete thought" if dur >= lo else "Short",
                        "topic_match": f"{kw_hits} topic keywords" if kw_hits else "No topic keywords",
                    },
                    "topic": ", ".join(sorted({t for t in toks if t in kws})[:3]) or "general",
                    "title": _title_from(segs[i]["text"]),
                    "description": text[:160].rsplit(" ", 1)[0] + ("..." if len(text) > 160 else ""),
                    "hashtags": ["#shorts"] + [f"#{k.lower().replace(' ', '')}" for k in keywords[:4]],
                    "key_words": [t for t in toks if t in kws][:3] or [t for t in toks if len(t) > 6][:2],
                })
            j += 1
    cands.sort(key=lambda c: -c["score"])
    out: list[dict] = []
    for c in cands:
        if any(overlap(c, k) > 0.0 for k in out):
            continue
        out.append(c)
        if len(out) >= n:
            break
    return out


def _title_from(text: str, limit: int = 60) -> str:
    t = re.sub(r"\s+", " ", text).strip().rstrip(".")
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "..."
    return t[:1].upper() + t[1:]
