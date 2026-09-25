"""Titles and hook cards get checked against the rules before they reach a clip."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import titles  # noqa: E402

CLIP = ("so there was this guy right and people online were arguing about him for years, nobody could agree on "
        "what actually happened that night or why he did any of it, and the police report was sealed. " * 2 +
        "turns out he killed his best friend because he believed it would make him a vampire.")


def test_a_good_title_passes():
    assert titles.problems("Uncovered: the Bible story about the Nephilim", CLIP) == []
    assert titles.problems("Why the Romans stopped building roads", CLIP) == []


def test_the_title_that_gives_the_ending_away_is_caught():
    bad = titles.problems("He killed his best friend to become a vampire", CLIP)
    assert any("ending" in b for b in bad)


def test_a_question_that_answers_itself_no_is_caught():
    assert any("no" in b for b in titles.problems("Is a real zombie outbreak happening?"))
    assert any("no" in b for b in titles.problems("Mushrooms boost fighter intuition?"))


def test_a_real_question_word_is_allowed():
    assert titles.problems("Joe Rogan said WHAT about mushrooms?") == []
    assert titles.problems("Why fighters swear by lion's mane mushrooms") == []


def test_a_title_about_nobody_is_caught():
    assert any("pronoun" in b for b in titles.problems("They covered up the whole thing for years"))


def test_the_clips_opening_words_are_not_a_title():
    text = "so there was this guy right and people online were arguing about him for years and nobody agreed"
    assert any("opening words" in b for b in titles.problems("so there was this guy right and people online", text))


def test_length_and_filler_rules():
    assert any("70" in b for b in titles.problems("x" * 80))
    assert any("short" in b for b in titles.problems("Wild"))
    assert any("filler" in b for b in titles.problems("You need to hear this one"))


def test_hooks_must_not_close_the_gap_they_open():
    assert any("ending" in b for b in titles.hook_problems("he killed his best friend to become a vampire", CLIP))
    assert titles.hook_problems("no way it gets this dark...", CLIP) == []


def test_a_hook_too_long_to_read_in_three_seconds():
    assert any("words" in b for b in titles.hook_problems("this is a very long hook card that nobody could possibly read in time"))


def test_a_hook_that_is_just_the_title_again():
    assert any("same as the title" in b for b in titles.hook_problems("the truth about Rome", "", "The truth about Rome"))


def test_fix_title_keeps_a_good_one_untouched():
    good = "Uncovered: the Bible story about the Nephilim"
    assert titles.fix_title(good, CLIP) == good


def test_fix_title_replaces_a_broken_one_with_something_valid():
    out = titles.fix_title("He killed his best friend to become a vampire", CLIP, keywords=["vampire"])
    assert out != "He killed his best friend to become a vampire"
    assert titles.problems(out, CLIP) == [], f"the replacement broke the rules too: {out}"


def test_fix_hook_replaces_a_spoiler_with_a_gap():
    out = titles.fix_hook("he killed his best friend to become a vampire", CLIP)
    assert titles.hook_problems(out, CLIP) == [], f"the replacement broke the rules too: {out}"


def test_polish_all_without_claude_still_fixes_everything(monkeypatch):
    from clipforge import llm
    monkeypatch.setattr(llm, "mode", lambda: "mock")
    moments = [{"title": "He killed his best friend to become a vampire", "hook": "Is that even possible?", "text": CLIP},
               {"title": "Uncovered: the Bible story about the Nephilim", "hook": "no way it gets this dark...", "text": CLIP}]
    fixed = titles.polish_all(moments)
    assert fixed == 1, "only the broken one should be touched"
    assert titles.problems(moments[0]["title"], CLIP) == []
    assert titles.hook_problems(moments[0]["hook"], CLIP, moments[0]["title"]) == []
    assert moments[0]["title_was"] == "He killed his best friend to become a vampire"
    assert moments[1]["title"] == "Uncovered: the Bible story about the Nephilim"


def test_polish_all_uses_claudes_rewrite_when_it_is_good(monkeypatch):
    monkeypatch.setattr(titles, "rewrite", lambda jobs: {
        0: {"index": 0, "title": "Why he believed killing his friend would work", "hook": "the part nobody explains..."}})
    moments = [{"title": "He killed his best friend to become a vampire", "hook": "Is that even possible?", "text": CLIP}]
    titles.polish_all(moments)
    assert moments[0]["title"] == "Why he believed killing his friend would work"
    assert moments[0]["hook"] == "the part nobody explains..."


def test_a_bad_rewrite_from_claude_is_rejected_too(monkeypatch):
    monkeypatch.setattr(titles, "rewrite", lambda jobs: {
        0: {"index": 0, "title": "Is this even real?", "hook": "he killed his best friend to become a vampire"}})
    moments = [{"title": "He killed his best friend to become a vampire", "hook": "Is that even possible?", "text": CLIP}]
    titles.polish_all(moments)
    assert titles.problems(moments[0]["title"], CLIP) == [], "a bad rewrite must not be accepted"
    assert titles.hook_problems(moments[0]["hook"], CLIP, moments[0]["title"]) == []
