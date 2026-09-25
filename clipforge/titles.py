"""Titles and hook cards, and the rules that throw out the bad ones.

A title has to make someone tap. A hook card has to make them stay for three seconds. They fail in
ways you can actually test for, and the three worst are:

  giving the ending away   "He killed his best friend to become a vampire"  (why watch now?)
  a question with a no     "Is a real zombie outbreak happening?"           ("no", scroll)
  nobody in it             "He", "they", "this guy" and no name             (about who?)

Everything Claude writes and everything the fallback builds goes through here before it reaches a clip.
"""
from __future__ import annotations
import re

MAX_TITLE = 70
MAX_HOOK_WORDS = 8
# a question opening with one of these can be answered "no" without watching
YES_NO = re.compile(r"^\s*(is|are|was|were|did|does|do|can|could|will|would|has|have|had|should|am)\b", re.I)
# a hook or title that starts on one of these is about nobody
BARE_PRONOUN = re.compile(r"^\s*(he|she|they|it|him|her|them|this guy|that guy|this man|this woman|the guy)\b", re.I)
# a real question has a question word somewhere. "Mushrooms boost fighter intuition?" is a statement wearing a
# question mark, and it answers itself with "no" just like "Is a zombie outbreak happening?" does.
WH = re.compile(r"\b(what|why|how|who|whose|whom|when|where|which)\b", re.I)
# "Why he did it" names the ending but still withholds the answer, so it is not a spoiler
WH_START = re.compile(r"^\s*(what|why|how|who|when|where|which)\b", re.I)
DEAD = {"you need to hear this one", "wait for the ending...", "did you know this??", "this changes everything",
        "this theory changes everything...", "nobody talks about this"}
FILLER = set("""a an the and or but so if of to in on at for with from by about into over after before this that these those it its
is are was were be been being do does did have has had you your yeah um uh okay like just really very""".split())

TITLE_RULES = """Title rules, in order of how much they matter:
1. Never give away the payoff. The title sells the question, the clip gives the answer. "He killed his best
   friend to become a vampire" is a bad title because there is nothing left to watch for.
2. Never a question that can be answered "no" without watching. "Is a real zombie outbreak happening?" gets a
   "no" and a scroll. Ask what, why, how, or who instead.
3. Name somebody or something. "He said WHAT?" is about nobody. Use the name, the place, the book, the event.
4. Built from what the clip is ABOUT, never its opening words, and never a straight summary.
5. Honest. Theories are framed as theories. Nothing implied that was not said.
Shapes that work: "Uncovered: the Bible story about X", "X said WHAT about Y?", "The truth about X nobody
talks about", "Why X actually happened", "What X got wrong about Y". Max 70 characters, no quote marks."""

HOOK_RULES = """Hook card rules (3-9 words, shown for the first 3 seconds, casual spoken tone):
It opens the gap the clip closes. It must NOT contain the answer, the punchline or the ending — if someone
could read the card and skip the clip, it has failed. No question that can be answered "no". No bare "he" or
"they" with no name. Sounds like a person talking, not a headline: "no way it gets this dark...",
"nobody talks about this part", "the bit they cut out", "this is where it gets weird". Never a summary."""


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", (text or "").lower()) if w not in FILLER and len(w) > 2]


def spoils(candidate: str, clip_text: str, tail: float = 0.3) -> bool:
    """True when the card is made of the clip's ending: reading it means you already know how it lands."""
    cw = set(words(candidate))
    if len(cw) < 2:
        return False
    toks = (clip_text or "").split()
    if len(toks) < 30:
        return False
    ending = set(words(" ".join(toks[int(len(toks) * (1 - tail)):])))
    opening = set(words(" ".join(toks[:int(len(toks) * 0.4)])))
    only_in_ending = (cw & ending) - opening
    return len(only_in_ending) >= 2 and len(only_in_ending) / len(cw) >= 0.5


def problems(title: str, clip_text: str = "") -> list[str]:
    """What is wrong with this title. Empty list means it is fine."""
    t = (title or "").strip()
    out = []
    if len(t) < 12:
        out.append("too short to say anything")
    if len(t) > MAX_TITLE:
        out.append(f"longer than {MAX_TITLE} characters")
    if t.lower().strip(" .?!") in DEAD:
        out.append("a filler line, not a title")
    if t.rstrip().endswith("?") and (YES_NO.match(t) or not WH.search(t)):
        out.append("a question that answers itself 'no': ask what, why, how or who instead")
    if BARE_PRONOUN.match(t):
        out.append("starts on a pronoun, so it is about nobody")
    if '"' in t or "“" in t:
        out.append("has quote marks")
    if clip_text and t.lower().rstrip(" .?!#") and clip_text.lower().lstrip().startswith(t.lower().rstrip(" .?!#")[:25]):
        out.append("just the clip's opening words")
    if clip_text and not WH_START.match(t) and spoils(t, clip_text):
        out.append("gives the ending away")
    return out


def hook_problems(hook: str, clip_text: str = "", title: str = "") -> list[str]:
    h = (hook or "").strip()
    out = []
    n = len(h.split())
    if n < 3:
        out.append("too short")
    if n > MAX_HOOK_WORDS:
        out.append(f"more than {MAX_HOOK_WORDS} words: it will not be read in 3 seconds")
    if h.lower().strip(" .?!") in DEAD:
        out.append("a filler line used on every clip")
    if h.rstrip().endswith("?") and (YES_NO.match(h) or not WH.search(h)):
        out.append("a question that answers itself 'no': ask what, why, how or who instead")
    if BARE_PRONOUN.match(h):
        out.append("starts on a pronoun, so it is about nobody")
    if title and h.lower().strip(" .?!") == title.lower().strip(" .?!"):
        out.append("the same as the title")
    if clip_text and not WH_START.match(h) and spoils(h, clip_text):
        out.append("gives the ending away, so there is nothing to stay for")
    return out


# ----------------------------------------------------------------------------- building better ones
def _subject(text: str, keywords: list[str] | None = None, n: int = 2) -> list[str]:
    from .moments import subject_words
    return subject_words(text or "", keywords, n)


def fix_title(title: str, clip_text: str, keywords: list[str] | None = None, fact_type: str = "") -> str:
    """Keep a good title. Rebuild a broken one around what the clip is actually about."""
    if title and not problems(title, clip_text):
        return title.strip()
    subj = _subject(clip_text, keywords)
    low = (clip_text or "").lower()
    a = (subj[0] if subj else "").strip()
    a_cap = a[:1].upper() + a[1:] if a else ""
    # a real name appears capitalised in the middle of a sentence; a word capitalised only because it
    # started one is not a name, and templates built on it read like nonsense
    named = [w for w in subj if re.search(r"(?<![.!?]\s)(?<!^)\b" + re.escape(w[:1].upper() + w[1:]) + r"\b", clip_text or "")]
    b = subj[1] if len(subj) > 1 and subj[1] in named else ""
    if not a:
        return (title or "The part of this story nobody tells")[:MAX_TITLE]
    if fact_type == "faith" or any(k in low for k in ("bible", "jesus", "god", "scripture", "verse")):
        t = f"Uncovered: the Bible story about {a}"
    elif re.search(r"\b" + re.escape(a) + r"\b\s+(?:\w+\s+){0,2}(said|says|told|claims|admitted)\b", clip_text or "", re.I) and b:
        t = f"{a_cap} said WHAT about {b}?"
    elif fact_type == "theory" or any(k in low for k in ("theory", "conspiracy", "secret", "hidden", "cover up")):
        t = f"The {a} theory nobody talks about"
    elif b and a in named:
        t = f"What {a_cap} really has to do with {b}"
    else:
        t = f"The truth about {a} nobody talks about"
    return t[:MAX_TITLE]


def fix_hook(hook: str, clip_text: str, title: str = "", fact_type: str = "", keywords: list[str] | None = None) -> str:
    """Keep a good hook. Rebuild a broken one so it opens a gap instead of closing it."""
    if hook and not hook_problems(hook, clip_text, title):
        return hook.strip()
    subj = _subject(clip_text, keywords, 1)
    a = (subj[0] if subj else "").strip()
    low = (clip_text or "").lower()
    if fact_type == "faith" or any(k in low for k in ("bible", "jesus", "god ", "scripture")):
        return f"the {a} part they skip..." if a else "they never taught you this part..."
    if fact_type == "theory" or any(k in low for k in ("theory", "conspiracy", "aliens", "secret", "hidden")):
        return f"nobody talks about this {a} bit..." if a else "this is where it gets weird..."
    if any(k in low for k in ("dark", "died", "death", "killed", "murder", "creepy", "scary")):
        return "no way it gets this dark..."
    if any(k in low for k in ("crazy", "insane", "wild", "unbelievable")):
        return f"wait til you hear the {a} part" if a else "wait til you hear this part"
    return f"the {a} bit nobody mentions..." if a else "stay for the last bit..."


# ----------------------------------------------------------------------------- asking Claude to try again
REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "title": {"type": "string"}, "hook": {"type": "string"}},
        "required": ["index", "title", "hook"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}

REWRITE_SYSTEM = ("You rewrite YouTube Shorts titles and hook cards that broke the channel's rules. "
                  "You are given what was written, exactly what is wrong with it, and the clip's transcript. "
                  "Fix the problem without inventing anything that was not said.\n\n" + TITLE_RULES + "\n\n" + HOOK_RULES +
                  "\nReturn JSON only: one item per index, with a title and a hook for each.")


def rewrite(jobs: list[dict]) -> dict[int, dict]:
    """Ask Claude for better titles and hooks for the ones that failed. {} when Claude is not available."""
    from . import llm
    if not jobs or llm.mode() != "live":
        return {}
    parts = []
    for j in jobs:
        parts.append(
            f"--- index {j['index']} ---\n"
            f"Title written: {j.get('title') or '(none)'}\n"
            f"What is wrong with it: {'; '.join(j.get('problems') or []) or 'nothing, keep the meaning'}\n"
            f"Hook written: {j.get('hook') or '(none)'}\n"
            f"What is wrong with it: {'; '.join(j.get('hook_problems') or []) or 'nothing, keep the meaning'}\n"
            f"Transcript:\n{(j.get('text') or '')[:2500]}\n")
    try:
        out = llm.ask_json("Rewrite the title and hook for each of these clips.\n\n" + "\n".join(parts),
                           system=REWRITE_SYSTEM, model=None, schema=REWRITE_SCHEMA, max_tokens=2000, effort="low")
    except llm.LLMError:
        return {}
    return {int(i["index"]): i for i in (out or {}).get("items", []) if "index" in i}


def polish_all(moments: list[dict], keywords: list[str] | None = None) -> int:
    """Check every title and hook. Claude rewrites the broken ones; templates catch what it cannot. Returns how many were fixed."""
    jobs = []
    for i, m in enumerate(moments):
        text = m.get("text") or ""
        tp = problems(str(m.get("title") or ""), text)
        hp = hook_problems(str(m.get("hook") or ""), text, str(m.get("title") or ""))
        if tp or hp:
            jobs.append({"index": i, "text": text, "title": m.get("title"), "problems": tp,
                         "hook": m.get("hook"), "hook_problems": hp})
    if not jobs:
        return 0
    better = rewrite(jobs)
    for j in jobs:
        m = moments[j["index"]]
        text = m.get("text") or ""
        got = better.get(j["index"]) or {}
        if j["problems"]:
            m["title_was"], m["title_problems"] = m.get("title"), j["problems"]
            cand = (got.get("title") or "").strip()
            m["title"] = cand if cand and not problems(cand, text) else fix_title(cand or m.get("title"), text, keywords)
        if j["hook_problems"]:
            m["hook_was"], m["hook_problems"] = m.get("hook"), j["hook_problems"]
            cand = (got.get("hook") or "").strip()
            m["hook"] = cand if cand and not hook_problems(cand, text, m.get("title", "")) else \
                fix_hook(cand or m.get("hook"), text, m.get("title", ""), keywords=keywords)
    return len(jobs)
