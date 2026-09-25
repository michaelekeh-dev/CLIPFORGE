"""The last check before a clip is allowed to post by itself.

Two things ruin an otherwise good clip and neither shows up in a virality score:

  the thought is cut in half   "did you know that the Romans actually—"  and it ends
  the framing is wrong         a forehead sliced off, or nobody on screen at all

So every finished clip gets read back: its words against the transcript, and its own rendered frames
against a face detector. A clip that fails is never auto-posted — it waits in Telegram with the reason
written out, so you can look once and decide.
"""
from __future__ import annotations
import re
from . import db
from .config import cfg

# a sentence that stops on one of these is not finished, whatever the punctuation says
DANGLING = set("""and but so or because that which who when while if as than then for to of in on at with from by about into
is are was were am be been being do does did doing have has had having will would can could should may might must
the a an my your his her its our their this that these those i you he she we they it there here what how why
like just really very and/or plus versus vs
""".split())
# "did you know that ..." with nothing after it is the exact failure the channel owner described
SETUP_OPENERS = re.compile(r"\b(did you know|you know what|here'?s the thing|the crazy part is|what people don'?t know|"
                           r"let me tell you|the thing is|imagine this|picture this|get this)\b", re.I)
SENTENCE_END = re.compile(r"[.!?…]\"?\s*$")
# how close the next spoken word has to be for the cut to count as mid-speech
STILL_TALKING = 0.35
FACE_SAMPLES = 14


def _clip_words(words: list[dict], start: float, end: float) -> list[dict]:
    return [w for w in words if w["e"] > start + 0.01 and w["s"] < end - 0.01]


def speech_problems(words: list[dict], start: float, end: float, text: str = "") -> list[dict]:
    """Does the clip start and finish on a finished thought? Reads the transcript, not the audio."""
    out = []
    if not words:
        return out
    inside = _clip_words(words, start, end)
    if not inside:
        return [{"code": "no_speech", "severity": "bad", "detail": "No speech inside the clip."}]
    body = text.strip() or " ".join(w["w"] for w in inside)
    # some speech models return no punctuation at all. Without full stops we cannot tell a finished
    # sentence from an interrupted one, so we still flag the timing but never block a post on a guess.
    whole = " ".join(w["w"] for w in words[:4000])
    punctuated = len(re.findall(r"[.!?]", whole)) >= max(1, len(whole.split()) // 60)
    hard = "bad" if punctuated else "check"
    last = inside[-1]
    after = next((w for w in words if w["s"] >= last["e"] - 0.01), None)
    before = next((w for w in reversed(words) if w["e"] <= inside[0]["s"] + 0.01), None)

    # 1. cut off mid-speech: someone is still talking when the clip stops
    if after and after["s"] - end < STILL_TALKING and not SENTENCE_END.search(body):
        out.append({"code": "ends_mid_sentence", "severity": hard,
                    "detail": f"Still talking when it stops — the next word \"{after['w']}\" comes "
                              f"{after['s'] - end:.2f}s later. Ends on: \"{_tail(body)}\""})
    # 2. finished speaking, but on a word no sentence can end on
    tail_word = re.sub(r"[^a-z']", "", (inside[-1]["w"] or "").lower())
    if tail_word in DANGLING and not any(p["code"] == "ends_mid_sentence" for p in out):
        out.append({"code": "dangling_end", "severity": "bad",  # a conjunction is a conjunction, punctuation or not
                    "detail": f"Ends on \"{tail_word}\", which leaves the sentence hanging: \"{_tail(body)}\""})
    # 3. a setup with no payoff: "did you know that..." and then it stops
    m = SETUP_OPENERS.search(body)
    if m:
        rest = body[m.end():]
        if len(rest.split()) < 12:
            out.append({"code": "setup_without_payoff", "severity": "bad",
                        "detail": f"Opens with \"{m.group(0)}\" and stops {len(rest.split())} words later, "
                                  "before the point is made."})
    # 4. the last thing said is a question nobody answers
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    if sentences and sentences[-1].rstrip().endswith("?") and len(sentences) > 1:
        out.append({"code": "ends_on_a_question", "severity": "check",
                    "detail": f"The clip's last line is a question that never gets answered: \"{_tail(body)}\""})
    # 5. starts mid-sentence: someone was already talking, and what they said has no full stop on it
    if before:
        bi = next((k for k, w in enumerate(words) if w is before), None)
        lead = " ".join(w["w"] for w in words[max(0, (bi or 0) - 6):(bi or 0) + 1])
        gap = inside[0]["s"] - before["e"]
        if gap < STILL_TALKING and not SENTENCE_END.search(lead):
            out.append({"code": "starts_mid_sentence", "severity": "check",
                        "detail": f"Starts while a sentence is already running — \"{before['w']}\" is said "
                                  f"{gap:.2f}s before the clip begins."})
    return out


def _tail(text: str, words_n: int = 9) -> str:
    return " ".join(text.split()[-words_n:])


def face_problems(path: str, samples: int = FACE_SAMPLES, skip_start: float = 0.0, skip_end: float = 0.0,
                  skip_windows: list[tuple[float, float]] | None = None) -> list[dict]:
    """Sample the finished vertical clip and check every face is whole and on screen.

    The intro and outro cards and any B-roll are skipped: there is meant to be nobody on screen there,
    so counting them would fail every clip that uses them."""
    try:
        import cv2
        import numpy as np  # noqa: F401
        from .reframe import FaceFinder
    except Exception as e:  # noqa: BLE001
        return [{"code": "face_check_unavailable", "severity": "check", "detail": str(e)[:150]}]
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return [{"code": "unreadable", "severity": "bad", "detail": "The rendered clip could not be opened."}]
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if not total or not w or not h:
        cap.release()
        return [{"code": "unreadable", "severity": "bad", "detail": "The rendered clip has no frames."}]
    # the cards and the B-roll are meant to have nobody in them, so they are not evidence of bad framing
    skip = [(max(0.0, s0), e0) for s0, e0 in (skip_windows or []) if e0 > s0]
    first, last = int(skip_start * fps), max(1, total - int(skip_end * fps) - 1)
    if last <= first:
        first, last = 0, max(1, total - 1)
    span = max(1, last - first)
    idxs = []
    for i in range(samples):
        fi = int(first + i * span / max(1, samples - 1))
        if any(s0 * fps <= fi <= e0 * fps for s0, e0 in skip):
            continue
        idxs.append(fi)
    if not idxs:
        return []
    finder = FaceFinder(w, h)
    seen, cut_top, cut_side, small, checked = 0, 0, 0, 0, 0
    top_margin = h * float(cfg.get("review.headroom_fraction", 0.02))
    side_margin = w * 0.01
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        checked += 1
        faces = [f for f in finder(frame) if f[4] > 0.6]
        if not faces:
            continue
        seen += 1
        f = max(faces, key=lambda f: f[2] * f[3])
        x, y, fw, fh = f[0], f[1], f[2], f[3]
        if y <= top_margin:
            cut_top += 1
        if x <= side_margin or (x + fw) >= w - side_margin:
            cut_side += 1
        if fh < h * float(cfg.get("review.min_face_height_fraction", 0.07)):
            small += 1
    cap.release()
    out = []
    if not checked:
        return [{"code": "unreadable", "severity": "bad", "detail": "No frames could be read back."}]
    face_share = seen / checked
    if face_share < float(cfg.get("review.min_face_share", 0.45)):
        out.append({"code": "no_face", "severity": "bad",
                    "detail": f"A face is only visible in {face_share:.0%} of the clip."})
    if seen and cut_top / seen > 0.25:
        out.append({"code": "face_cut_top", "severity": "bad",
                    "detail": f"The top of the head is cut off in {cut_top / seen:.0%} of the frames with a face."})
    if seen and cut_side / seen > 0.3:
        out.append({"code": "face_at_edge", "severity": "check",
                    "detail": f"The face touches the edge of the frame in {cut_side / seen:.0%} of the frames."})
    if seen and small / seen > 0.5:
        out.append({"code": "face_too_small", "severity": "check",
                    "detail": "The speaker is small in frame for most of the clip."})
    return out


def verdict(problems: list[dict]) -> str:
    if any(p["severity"] == "bad" for p in problems):
        return "bad"
    if problems:
        return "check"
    return "good"


def check(clip: dict, words: list[dict] | None = None, path: str | None = None, lead_in: float = 0.0,
          lead_out: float = 0.0, skip_windows: list[tuple[float, float]] | None = None) -> dict:
    """Everything, on one finished clip. Returns {verdict, problems, checked}."""
    problems: list[dict] = []
    checked = []
    data = clip.get("data") or {}
    if words:
        problems += speech_problems(words, float(clip["start"]), float(clip["end"]), data.get("text", ""))
        checked.append("speech")
    p = path or clip.get("path")
    if p and bool(cfg.get("review.check_faces", True)):
        problems += face_problems(p, skip_start=lead_in, skip_end=lead_out, skip_windows=skip_windows or [])
        checked.append("faces")
    return {"verdict": verdict(problems), "problems": problems, "checked": checked}


def line(rev: dict) -> str:
    """One line for Telegram or the web page."""
    if not rev:
        return ""
    mark = {"good": "🟢", "check": "🟡", "bad": "🔴"}.get(rev.get("verdict", ""), "")
    if rev.get("verdict") == "good":
        return f"{mark} Looks clean: finishes its thought, faces fine."
    return mark + " " + "; ".join(p["detail"] for p in rev.get("problems", [])[:3])


def blocks_autopost(rev: dict) -> bool:
    return (rev or {}).get("verdict") == "bad"
