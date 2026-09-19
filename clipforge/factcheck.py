"""Fact check each clip so nothing misleading gets posted. Claude Haiku when live, a careful heuristic otherwise."""
from __future__ import annotations
import re
from .config import cfg
from . import llm

SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["fact", "theory", "opinion", "faith", "entertainment"]},
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "claim": {"type": "string"},
            "status": {"type": "string", "enum": ["well-supported", "disputed", "false", "unverifiable"]},
            "note": {"type": "string"}}, "required": ["claim", "status", "note"], "additionalProperties": False}},
        "scripture": {"type": "array", "items": {"type": "object", "properties": {
            "reference": {"type": "string"}, "in_context": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["reference", "in_context", "note"], "additionalProperties": False}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "honest_title": {"type": "string"},
        "verdict": {"type": "string", "enum": ["ok", "needs context", "skip"]},
        "summary": {"type": "string"},
    },
    "required": ["type", "claims", "scripture", "red_flags", "honest_title", "verdict", "summary"],
    "additionalProperties": False,
}

SYSTEM = """You are a careful fact checker for a YouTube channel that posts theories, history and Christian faith content.
The channel owner never wants to post something misleading. Be fair: theories are fine when framed as theories,
faith content is fine when it is presented as belief. Flag speculation presented as fact, harmful conspiracy tropes
(for example antisemitic ones), health misinformation, and accusing real people of crimes without evidence.
For scripture: list verses quoted or referenced, say whether they are used in context, and flag end-times date-setting.
Keep every text field short. Return JSON only."""


def check_clip(text: str, title: str) -> dict:
    if llm.mode() == "live":
        prompt = (f"Proposed title: {title}\n\nClip transcript:\n{text}\n\n"
                  "Return: type (fact/theory/opinion/faith/entertainment); claims: the main claims each marked "
                  "well-supported / disputed / false / unverifiable with a short note; scripture: verses quoted or "
                  "referenced with in_context true/false and a note; red_flags: short strings; honest_title: catchy but "
                  "honest, max 70 characters, framing theories as theories; verdict: ok / needs context / skip; "
                  "summary: one sentence a busy person can read. For honest_title keep the proposed title's style (a Shorts title "
                  "about the subject, e.g. 'Uncovered: the Bible story about X' or 'X said WHAT about Y?'), fix it only if it "
                  "is misleading; never replace it with the clip's opening words.")
        try:
            out = llm.ask_json(prompt, system=SYSTEM, model=cfg.get("llm.check_model"), schema=SCHEMA, max_tokens=2000)
            out["checked_by"] = cfg.get("llm.check_model")
            out["honest_title"] = (out.get("honest_title") or title)[:70]
            return out
        except llm.LLMError as e:
            out = heuristic_check(text, title)
            out["summary"] = f"Not checked by Claude ({e}). " + out["summary"]
            return out
    out = heuristic_check(text, title)
    return out


FAITH = {"god", "jesus", "christ", "bible", "scripture", "lord", "faith", "prayer", "church", "heaven", "holy", "spirit",
         "gospel", "prophecy", "rapture", "sin", "salvation"}
THEORY = {"theory", "aliens", "alien", "ufo", "conspiracy", "secret", "hidden", "cover", "illuminati", "elite", "they",
          "ancient", "pyramids", "giants", "nephilim", "simulation"}
HEALTH = {"cure", "cures", "vaccine", "vaccines", "cancer", "doctors", "medicine", "heal", "healing", "disease"}
HATE = {"jews", "jewish", "zionist", "zionists", "rothschild", "rothschilds", "globalists"}
CRIME = {"pedophile", "pedophiles", "murdered", "murder", "killed", "trafficking", "rapist", "criminal"}
DATE_SET = re.compile(r"\b(20\d\d|next year|this year|in \d+ (days|weeks|months|years))\b.*\b(rapture|return|end of the world|tribulation|second coming)\b|\b(rapture|return|end of the world|tribulation|second coming)\b.*\b(20\d\d|next year|this year)\b", re.I)
VERSE = re.compile(r"\b((?:1|2|3|First|Second|Third)?\s?[A-Z][a-z]+)\s(\d{1,3})(?::(\d{1,3})(?:-(\d{1,3}))?)?\b")
BOOKS = {"genesis", "exodus", "leviticus", "numbers", "deuteronomy", "joshua", "judges", "ruth", "samuel", "kings",
         "chronicles", "ezra", "nehemiah", "esther", "job", "psalm", "psalms", "proverbs", "ecclesiastes", "isaiah",
         "jeremiah", "lamentations", "ezekiel", "daniel", "hosea", "joel", "amos", "obadiah", "jonah", "micah", "nahum",
         "habakkuk", "zephaniah", "haggai", "zechariah", "malachi", "matthew", "mark", "luke", "john", "acts", "romans",
         "corinthians", "galatians", "ephesians", "philippians", "colossians", "thessalonians", "timothy", "titus",
         "philemon", "hebrews", "james", "peter", "jude", "revelation", "revelations"}


def heuristic_check(text: str, title: str) -> dict:
    toks = re.findall(r"[a-zA-Z']+", text.lower())
    tset = set(toks)
    f, th = len(tset & FAITH), len(tset & THEORY)
    if f and f >= th:
        typ = "faith"
    elif th:
        typ = "theory"
    elif any(t in tset for t in ("think", "believe", "feel", "opinion")):
        typ = "opinion"
    else:
        typ = "entertainment"
    flags = []
    if tset & HATE:
        flags.append("Mentions groups often targeted by conspiracy tropes. Check it is not antisemitic framing.")
    if tset & HEALTH and (tset & {"cure", "cures", "heal", "healing"}):
        flags.append("Health claim. Do not present cures as fact.")
    if tset & CRIME and re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", text):
        flags.append("Names a person near crime words. Do not accuse real people without evidence.")
    if DATE_SET.search(text):
        flags.append("Sounds like end-times date-setting.")
    scripture = []
    for m in VERSE.finditer(text):
        book = m.group(1).strip().lower().split()[-1]
        if book in BOOKS:
            scripture.append({"reference": m.group(0).strip(), "in_context": True, "note": "Not checked by Claude. Read the verse in context before posting."})
    # claims: declarative sentences with numbers or strong verbs
    claims = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        s = sent.strip()
        if 30 < len(s) < 200 and (re.search(r"\d", s) or re.search(r"\b(is|was|were|are|did|proves?|proof|fact)\b", s.lower())):
            claims.append({"claim": s[:140], "status": "unverifiable", "note": "Not checked by Claude yet."})
        if len(claims) >= 3:
            break
    honest = title[:70]
    if typ == "theory" and not re.search(r"theory|could|might|may|\?", honest, re.I):
        honest = ("Theory: " + honest)[:70]
    verdict = "needs context" if flags else "ok"
    summary = "Automatic check only (no Anthropic key): " + (
        "possible problems found, read before posting." if flags else "nothing obviously wrong, but claims were not verified.")
    return {"type": typ, "claims": claims, "scripture": scripture, "red_flags": flags, "honest_title": honest,
            "verdict": verdict, "summary": summary, "checked_by": "heuristic"}


def badge(verdict: str) -> str:
    return {"ok": "🟢", "needs context": "🟡", "skip": "🔴"}.get(verdict, "🟡")
