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

TITLE_STYLE = ("Title style (this matters a lot): a YouTube Shorts title that makes people tap, built from the CONTENT of the clip, "
               "never its opening words. Patterns that work: 'Uncovered: the Bible story about X', 'X said WHAT about Y?', "
               "'The truth about X nobody talks about', 'Why X actually happened', 'X explains Y in 30 seconds'. "
               "Name the subject (person, place, book, event). Max 70 characters, no quotes, honest, theories framed as theories.")

SYSTEM = """You are a senior short-form video editor. You pick the moments from a long talking video that would work
best as standalone vertical Shorts for a channel about theories, history and Christian faith content.

A moment is a WHOLE THOUGHT, not a soundbite. Setup, then the turn, then the payoff. If the clip
ends before the point lands, it is worthless however good the opening line was — a viewer who has
to go find the rest will just leave.

Rules for every moment:
- start on a strong first sentence (a hook, a question, a bold claim, a story opening), never mid-sentence
- end AFTER the payoff has been said: the answer to the question, the punchline, the conclusion, the
  "and that's why...". Never end on the setup. Never end mid-sentence.
- if the point takes longer than the suggested length, TAKE THE TIME. Going over is fine; cutting
  the payoff off is not. A complete 40-second story beats a 15-second fragment every time.
- it must make complete sense to someone who has not seen the rest of the video: no "like I said",
  no "that guy" with no introduction, no pronoun with nothing to point at
- never pick two moments that make the SAME POINT. Different timestamps are not enough — if two
  moments would leave a viewer with the same takeaway, keep only the better one and find something
  genuinely different for the other slot. Spread your picks across the whole part you were given.
- use the transcript timestamps exactly (seconds)
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
        taken: list[str] = []   # topics earlier parts already used — each chunk was blind to this
        for i, ch in enumerate(chunks):
            if progress:
                progress("Finding best moments", 42 + 12 * i / max(1, len(chunks)))
            # Every part was asked the same question with no memory, so on a show that circles one
            # subject all episode each part dutifully returned its own version of the same moment.
            avoid = (f"Parts before this one already covered: {'; '.join(taken[-12:])}. "
                     "Pick moments that are genuinely ABOUT something else — a different story, claim "
                     "or bit. Only repeat a subject if this part says something clearly new about it.\n"
                     if taken else "")
            prompt = (f"Video part {i + 1} of {len(chunks)} ({fmt_ts(ch['start'])} to {fmt_ts(ch['end'])}).\n"
                      f"Pick up to {per} of the best moments, each about {lo:.0f}-{hi:.0f} seconds — run over if "
                      "that is what it takes to include the payoff.\n"
                      + avoid +
                      f"Topic keywords the channel cares about (bonus, not a must): {', '.join(keywords) or 'none'}.\n"
                      "For each moment give: start and end in seconds (use the [timestamps]; end = the moment the LAST "
                      "SENTENCE YOU KEEP finishes — the one carrying the payoff, not the one before it), score 0-100 for "
                      "virality, one short sentence for each reason (hook, payoff, emotion, standalone, topic_match), "
                      "topic (3 words max), title (" + TITLE_STYLE + "), a 1-2 sentence description that says what the clip is about, "
                      "5-8 hashtags, key_words: 1-3 words spoken in the clip worth highlighting in the captions, and emojis: "
                      "up to 3 pairs of a spoken word plus one fitting emoji (spread out, none is fine), "
                      "hook: a short curiosity teaser (3-9 words) shown on a card for the first 3 seconds. It must fit THIS "
                      "clip's content and mood, be honest, and make people stay: for something dark 'no way it gets this dark...', "
                      "for a surprising fact 'did you know this??', for a theory 'this theory changes everything', for a story "
                      "'wait for the ending...'. Casual spoken tone, no summary, no clickbait lies. "
                      "zooms: 1-3 timestamps in seconds of punchlines or reveals worth a punch-in zoom, "
                      "broll: up to 2 moments where something visual is mentioned (a place, an object, an animal): "
                      "t = the timestamp in seconds, word = the spoken word, query = 2-3 word stock footage search.\n\n"
                      f"TRANSCRIPT:\n{chunk_text(ch)}")
            try:
                out = llm.ask_json(prompt, system=SYSTEM, model=cfg.get("llm.pick_model"), schema=MOMENT_SCHEMA)
                for m in out.get("moments", []):
                    cands.append(m)
                    t = str(m.get("topic") or "").strip()
                    if t and t.lower() not in [x.lower() for x in taken]:
                        taken.append(t)
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
    # ── THE CLIP MUST END WHERE THE THOUGHT ENDS ────────────────────────────────────────────
    # This gave up looking for the end of the sentence after 3 seconds, and then — the real
    # damage — the trim loop below walked the end back ONE WORD AT A TIME ("else j - 1") when
    # it could not find a sentence boundary that still met the minimum. Both roads end the clip
    # in the middle of a sentence, which is the "it cut 15 seconds in like it never heard the
    # full story" complaint: the setup is in, the payoff is not.
    #
    # Now: look further for the sentence end, and let a clip run OVER the maximum by a grace
    # window when the only thing standing between it and a complete thought is a few seconds.
    # A 38-second clip that lands its punchline beats a 30-second clip that does not.
    # ONE budget, not two. The first version of this had a separate "look ahead N seconds" window
    # AND the length ceiling, and the gap between them was a dead zone: a sentence ending exactly
    # at the look-ahead limit was neither reached nor rejected cleanly, so a good clip was thrown
    # away. How far we may run is `hi + grace` and nothing else.
    dur = lambda a, b: words[b]["e"] - words[a]["s"]
    grace = float(cfg.get("moments.overflow_grace", 8.0))
    last = len(words) - 1
    ends_clean = lambda k: bool(SENT_END.search(words[k]["w"])) or k >= last
    k = j
    while k + 1 < len(words) and not ends_clean(k) and dur(i, k + 1) <= hi + grace:
        k += 1
    if ends_clean(k):
        j = k
    # Too long, or still stranded mid-sentence: walk back to the LAST sentence end that is
    # still long enough. Never to a bare word — a clean 20 seconds beats a ragged 30.
    if dur(i, j) > hi + grace or not ends_clean(j):
        k = j
        while k > i and not (ends_clean(k) and lo <= dur(i, k) <= hi + grace):
            k -= 1
        if k > i and ends_clean(k) and dur(i, k) >= lo:
            j = k
        else:
            return None   # this candidate cannot be ended on a complete thought — drop it
    if dur(i, j) < max(3.0, lo * 0.6):
        return None
    pb, pa = float(cfg.get("moments.pad_before", 0.15)), float(cfg.get("moments.pad_after", 0.35))
    out = dict(m)
    out["start"] = round(max(0.0, words[i]["s"] - pb), 3)
    out["end"] = round(words[j]["e"] + pa, 3)
    out["wi"], out["wj"] = i, j
    # what was actually SAID — dedupe compares this, not the title the model wrote for it
    out["text"] = " ".join(w["w"] for w in words[i:j + 1])
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


STOP = {"that", "this", "they", "them", "then", "there", "with", "what", "when", "have", "been", "were", "your",
        "from", "just", "like", "know", "about", "would", "could", "because", "really", "thing", "things", "gonna",
        "want", "said", "says", "yeah", "okay", "right", "actually", "something", "everything", "people", "going"}


def _content(text: str) -> set[str]:
    """Content words only — the words that say what a clip is ABOUT.

    Contractions are folded to their stem first ("that's" -> "that", "there's" -> "there") or
    they survive the stop list as four-letter "content" and quietly pad the union, which drags
    the similarity of two near-identical clips down under the threshold."""
    out = set()
    for t in re.findall(r"[a-z']+", (text or "").lower()):
        t = re.sub(r"'(s|re|ve|ll|d|m|t)$", "", t).replace("'", "")
        if len(t) > 3 and t not in STOP:
            out.add(t)
    return out


def similarity(a: str, b: str) -> float:
    """Jaccard over content words. 0 = unrelated, 1 = the same thing said twice."""
    A, B = _content(a), _content(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _topic_key(m: dict) -> str:
    t = re.sub(r"[^a-z ]", "", str(m.get("topic", "")).lower()).strip()
    return "" if t in ("", "general", "none") else t


def _clip_text(m: dict) -> str:
    return m.get("text") or m.get("title") or m.get("description") or ""


def dedupe(ms: list[dict], n: int) -> list[dict]:
    """Keep the best n, and make sure they are not THE SAME CLIP THREE TIMES.

    This used to compare start/end only, so two moments twenty minutes apart could never look
    like duplicates however identical they were. On a podcast that circles one subject all
    episode that is exactly the failure mode: the highest-scoring moments are all the same
    idea, none of them overlap in time, and the picker happily ships three of them. (Reported
    on EP.304 — three clips, topics 'faith / theory / theory', hook cards reading "You know why
    that's a trend?", "You never heard that theory?", "Theory: you know, like they have to show
    you what they're doing". Different timestamps, one clip.)

    So a candidate is now dropped when it repeats an already-kept clip in TIME, in TOPIC, or in
    WORDS. Variety is a preference, not a quota: if the rules cannot fill n clips the passes
    below relax one rule at a time rather than hand back four clips when five were asked for.
    """
    max_sim = float(cfg.get("moments.max_similarity", 0.45))
    per_topic = int(cfg.get("moments.max_per_topic", 1))
    ms = sorted(ms, key=lambda m: -m["score"])

    def is_dupe(m: dict, k: dict) -> bool:
        """The same clip, by any of the three ways two clips can be the same."""
        if overlap(m, k) > 0.25:
            return True
        sim = similarity(_clip_text(m), _clip_text(k))
        if sim > max_sim:
            return True
        # the same subject AND much of the same wording: the topic label is a strong hint, so it
        # takes less word overlap to call it a repeat
        tm, tk = _topic_key(m), _topic_key(k)
        return bool(tm) and tm == tk and sim > max_sim * 0.6

    keep: list[dict] = []
    topics: dict[str, int] = {}
    # Two passes. The first also caps how many clips may share a topic — that cap is a
    # PREFERENCE for variety and gets dropped in the second pass so that asking for five clips
    # still returns five. `is_dupe` is NOT a preference and never relaxes: padding the list with
    # a clip we already have is the bug being fixed, not an acceptable fallback.
    for cap_topics in (True, False):
        for m in ms:
            if len(keep) >= n:
                break
            if any(m is k for k in keep):
                continue
            if any(is_dupe(m, k) for k in keep):
                continue
            t = _topic_key(m)
            if cap_topics and t and topics.get(t, 0) >= per_topic:
                continue
            keep.append(m)
            if t:
                topics[t] = topics.get(t, 0) + 1
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
                # THE BREVITY BIAS (fixed 2026-09-18). This divided by the real duration, so the
                # SHORTER a window was the higher its keyword density scored — two hits in 15
                # seconds beat four hits in 45. Combined with a floor of 15s that is a machine for
                # producing clips that stop before the point does. The floor of 20 means a window
                # cannot earn density points simply by being too short to say anything.
                kw_rate = kw_hits / max(dur, 20.0) * 30
                hook = sum(1 for t in first if t in HOOK_WORDS)
                emo = sum(1 for t in toks if t in EMOTION) / max(dur, 1) * 30
                qmark = segs[i]["text"].count("?")
                weak = 1 if first and first[0] in WEAK_START else 0
                density = len(toks) / max(dur, 1)
                score = 40 + 10 * min(kw_rate, 4) + 8 * min(hook, 3) + 4 * min(emo, 3) + 6 * min(qmark, 1) - 15 * weak
                score += 5 if 2.0 <= density <= 3.5 else 0
                # Ending mid-sentence is the single worst thing a clip can do, and it was priced
                # at six points — less than one keyword hit. It is the complaint.
                score -= 20 if not SENT_END.search(segs[j]["text"]) else 0
                score -= abs(dur - sweet) / 4
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
                    "title": _title_from(text, keywords),
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


STOP = set("""a an the and or but so if of to in on at for with from by about into over after before this that these those it its
is are was were be been being do does did have has had i you he she we they me him her us them my your his our their what which who
whom whose when where why how not no yes just like really very kind sort thing things gonna wanna got get go going yeah um uh okay
know think mean say said says one two there here then than too also because as up down out off again more most some any all
""".split())


def subject_words(text: str, keywords: list[str] | None = None, n: int = 2) -> list[str]:
    """The words a clip is about: capitalised names first, then topic keywords, then the most repeated long words."""
    toks = re.findall(r"[A-Za-z][A-Za-z']+", text)
    generic = {"bible", "god", "jesus", "christ", "lord", "christian", "christians", "church", "youtube", "tiktok", "instagram"}
    # capitalised words that are not sentence starts and not generic religious words: real names/places first
    starts = {m.group(1).lower() for m in re.finditer(r"(?:^|[.!?]\s+)([A-Za-z']+)", text)}
    names = [t for t in toks if t[0].isupper() and t.lower() not in STOP and t.lower() not in generic and t.lower() not in starts and len(t) > 2]
    names += [t for t in toks if t.lower() in generic and t.lower() not in starts]
    # most repeated name first (the clip is about what keeps coming up), ties by first appearance
    seen_n: dict[str, int] = {}
    for t in names:
        seen_n[t] = seen_n.get(t, 0) + 1
    names = sorted(dict.fromkeys(names), key=lambda t: (-seen_n[t], names.index(t)))
    kws = [k for k in (keywords or []) if re.search(r"\b" + re.escape(k) + r"\b", text, re.I)]
    freq: dict[str, int] = {}
    for t in toks:
        tl = t.lower()
        if len(tl) > 5 and tl not in STOP:
            freq[tl] = freq.get(tl, 0) + 1
    common = [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])]
    out: list[str] = []
    for w in names + kws + common:
        if w.lower() not in {o.lower() for o in out}:
            out.append(w)
        if len(out) >= n:
            break
    return out


def _title_from(text: str, keywords: list[str] | None = None, limit: int = 70) -> str:
    """Fallback title without Claude: built around the subject, never the opening words."""
    subj = subject_words(text, keywords)
    low = text.lower()
    if not subj:
        return "You need to hear this one"
    a = subj[0]
    a_cap = a if a[0].isupper() else a.capitalize()
    said = re.search(r"\b" + re.escape(a) + r"\b\s+(?:\w+\s+){0,2}(said|says|told|claims|admitted)\b", text, re.I)
    if said and len(subj) > 1:
        t = f"{a_cap} said WHAT about {subj[1]}?"
    elif any(k in low for k in ("bible", "jesus", "god", "scripture", "verse")):
        t = f"Uncovered: the Bible story about {a}"
    elif any(k in low for k in ("theory", "aliens", "secret", "hidden", "conspiracy")):
        t = f"The theory about {a} nobody talks about"
    elif len(subj) > 1:
        t = f"The truth about {a} and {subj[1]}"
    else:
        t = f"The truth about {a} nobody talks about"
    return t[:limit]
