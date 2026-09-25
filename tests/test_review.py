"""The gate before a clip is allowed to post itself: whole thoughts, and faces that are actually there."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import review, titles  # noqa: E402


def W(sentence, step=0.5, dur=0.4):
    return [{"w": w, "s": i * step, "e": i * step + dur} for i, w in enumerate(sentence.split())]


def codes(problems):
    return {p["code"] for p in problems}


# ----------------------------------------------------------------------------- whole thoughts
def test_a_finished_thought_passes():
    words = W("the Romans built roads that still stand today. and then they left")
    assert review.speech_problems(words, 0.0, 3.9, "the Romans built roads that still stand today.") == []


def test_ending_on_a_conjunction_is_caught():
    # he does stop talking, but on "and" — the sentence still has nowhere to land
    words = W("the Romans built roads that still stand today and")
    words.append({"w": "anyway", "s": 20.0, "e": 20.4})  # long pause, so this is not a mid-speech cut
    probs = review.speech_problems(words, 0.0, 4.4, "the Romans built roads that still stand today and")
    assert "dangling_end" in codes(probs)
    assert "ends_mid_sentence" not in codes(probs)
    assert review.verdict(probs) == "bad"


def test_being_cut_off_while_still_talking_is_caught():
    words = W("the Romans built roads that still stand across the whole empire today")
    # the clip stops after "across" but the next word lands 0.1s later
    probs = review.speech_problems(words, 0.0, 4.45, "the Romans built roads that still stand across")
    assert "ends_mid_sentence" in codes(probs)


def test_the_did_you_know_failure_the_owner_described():
    words = W("did you know that the Romans")
    probs = review.speech_problems(words, 0.0, 2.9, "did you know that the Romans")
    assert "setup_without_payoff" in codes(probs)
    assert review.verdict(probs) == "bad"


def test_a_setup_that_does_pay_off_is_fine():
    body = ("did you know that the Romans built roads with a concrete recipe we only worked out again "
            "in the last twenty years and some of them still carry traffic today.")
    words = W(body)
    probs = review.speech_problems(words, 0.0, len(body.split()) * 0.5, body)
    assert "setup_without_payoff" not in codes(probs)


def test_starting_mid_sentence_is_flagged_but_not_fatal():
    words = W("so anyway the Romans built roads that still stand today. next thing")
    probs = review.speech_problems(words, 1.0, 5.4, "the Romans built roads that still stand today.")
    assert "starts_mid_sentence" in codes(probs)
    assert review.verdict(probs) == "check", "a soft start is worth a look, not a block"


def test_an_unanswered_question_at_the_end_is_a_soft_flag():
    body = "the Romans built roads that still stand today. so why did they stop building them?"
    probs = review.speech_problems(W(body), 0.0, len(body.split()) * 0.5, body)
    assert "ends_on_a_question" in codes(probs)


def test_no_speech_at_all_is_bad():
    assert codes(review.speech_problems(W("hello there friend"), 100.0, 105.0, "")) == {"no_speech"}


# ----------------------------------------------------------------------------- faces
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "1.7", "clip_01.mp4")


@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="no rendered sample to read back")
def test_a_normal_talking_clip_passes_the_face_check():
    assert review.face_problems(SAMPLE, samples=8) == []


@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="no rendered sample to read back")
def test_skipping_every_frame_returns_nothing_rather_than_failing():
    # cards and B-roll cover the whole clip: there is nothing left to judge, so do not judge it
    assert review.face_problems(SAMPLE, samples=6, skip_windows=[(0, 10_000)]) == []


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    bad = tmp_path / "not-a-video.mp4"
    bad.write_bytes(b"nope")
    assert codes(review.face_problems(str(bad))) == {"unreadable"}


# ----------------------------------------------------------------------------- verdicts
def test_verdict_and_blocking():
    assert review.verdict([]) == "good"
    assert review.verdict([{"severity": "check"}]) == "check"
    assert review.verdict([{"severity": "check"}, {"severity": "bad"}]) == "bad"
    assert review.blocks_autopost({"verdict": "bad"}) is True
    assert review.blocks_autopost({"verdict": "check"}) is False
    assert review.blocks_autopost({}) is False


def test_the_line_explains_itself():
    rev = {"verdict": "bad", "problems": [{"code": "dangling_end", "severity": "bad", "detail": "Ends on \"and\"."}]}
    assert "🔴" in review.line(rev) and "Ends on" in review.line(rev)
    assert "🟢" in review.line({"verdict": "good", "problems": []})


# ----------------------------------------------------------------------------- the whole chain
def test_a_badly_cut_clip_is_held_back_from_auto_posting(tmp_path, monkeypatch):
    """The point of all of this: mode=auto must not post a clip that stops mid-thought."""
    import json
    from clipforge import db, autopilot, notify, youtube
    db.init_db()
    sent = []
    monkeypatch.setattr(notify, "send_text", lambda text, *a, **k: sent.append(text) or {"ok": True})
    monkeypatch.setattr(notify, "send_clip", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(youtube, "connected", lambda: True)
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "mode": "auto"})
    queued = []
    monkeypatch.setattr(autopilot, "queue_post", lambda cid: queued.append(cid))

    db.execute("DELETE FROM projects WHERE id='p_rev'")
    db.execute("DELETE FROM clips WHERE project_id='p_rev'")
    db.insert("projects", {"id": "p_rev", "title": "ep", "status": "done", "options": "{}",
                           "info": json.dumps({"pick_method": "claude"})})
    good = {"fact_check": {"verdict": "ok"}, "review": {"verdict": "good", "problems": []}}
    bad = {"fact_check": {"verdict": "ok"},
           "review": {"verdict": "bad", "problems": [{"code": "dangling_end", "severity": "bad",
                                                      "detail": 'Ends on "and".'}]}}
    for cid, data, score in (("c_good", good, 90), ("c_bad", bad, 95)):
        db.insert("clips", {"id": cid, "project_id": "p_rev", "status": "done", "start": 0, "end": 30,
                            "score": score, "title": cid, "path": "x.mp4", "data": json.dumps(data), "settings": "{}"})
    autopilot.on_project_done("p_rev")

    assert queued == ["c_good"], "only the clean clip should have been queued"
    assert any("held back" in t for t in sent), "you should be told why the other one was not posted"


def test_a_transcript_with_no_punctuation_does_not_block_every_clip():
    """Some speech models return no full stops. We cannot tell a clean ending from a cut one, so we
    must not sit there blocking every post on a guess."""
    words = W("the romans built roads that still stand today and then they left the island for good")
    probs = review.speech_problems(words, 0.0, 3.9, "the romans built roads that still stand today")
    assert "ends_mid_sentence" in codes(probs)
    assert review.verdict(probs) == "check", "unsure is not the same as bad"


def test_punctuated_transcripts_still_block_a_real_cut():
    words = W("the Romans built roads. they stand today because the concrete was mixed with seawater which")
    probs = review.speech_problems(words, 0.0, 4.45, "the Romans built roads. they stand today because the concrete")
    assert review.verdict(probs) == "bad"
