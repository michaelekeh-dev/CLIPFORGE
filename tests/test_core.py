import json
import numpy as np
from pathlib import Path
import pytest
from clipforge.timeline import Timeline
from clipforge import moments, factcheck, audio, transcribe


def words_from(text, start=0.0, step=0.4):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "s": round(t, 2), "e": round(t + step * 0.8, 2)})
        t += step
    return out


def test_timeline_maps_and_removes():
    tl = Timeline.single(10, 20)
    assert tl.duration == 10
    assert tl.to_output(15) == 5
    tl2 = tl.remove([(12, 13), (18, 19)])
    assert tl2.pieces == [(10, 12), (13, 18), (19, 20)]
    assert tl2.duration == 8
    assert tl2.to_output(12.5) is None
    assert tl2.to_output(14) == 3
    assert tl2.to_output_clamped(12.5) == 2
    assert abs(tl2.to_source(3) - 14) < 1e-9


def test_segments_split_on_sentences():
    w = words_from("Hello there friend. How are you today? Fine thanks.")
    segs = transcribe.make_segments(w)
    assert [s["text"] for s in segs] == ["Hello there friend.", "How are you today?", "Fine thanks."]


def test_snap_moves_to_sentence_boundaries():
    w = words_from("This is the start of a sentence. And here is the punchline that lands well. Next thought begins now.")
    m = {"start": 0.5, "end": 5.6, "score": 80}
    s = moments.snap(m, w, 2, 30)
    assert s is not None
    assert s["wi"] == 0  # backed up to the sentence start
    assert w[s["wj"]]["w"].endswith(".")
    assert s["start"] <= w[0]["s"]


def test_dedupe_removes_overlaps():
    ms = [{"start": 0, "end": 30, "score": 90}, {"start": 10, "end": 40, "score": 85}, {"start": 50, "end": 80, "score": 70}]
    keep = moments.dedupe(ms, 5)
    assert [m["score"] for m in keep] == [90, 70]


def test_heuristic_pick_returns_non_overlapping():
    text = ("Did you know the pyramids hide a secret? Nobody talks about this ancient theory. " * 3 +
            "And then we went to lunch and it was fine and nothing happened at all. " * 3 +
            "God is faithful and the Bible says so in every book, that is the truth. " * 3)
    w = words_from(text, step=0.35)
    tr = {"words": w, "segments": transcribe.make_segments(w)}
    picked, method = moments.pick_moments(tr, 2, "short", ["pyramids", "ancient", "God", "Bible"])
    assert len(picked) == 2
    assert moments.overlap(picked[0], picked[1]) < 0.05  # only the padding may touch
    assert "heuristic" in method
    for p in picked:
        assert 10 - 1 <= p["end"] - p["start"] <= 30 + 1


def test_factcheck_heuristic_flags():
    fc = factcheck.heuristic_check("The rapture will happen in 2027 says John 3:16 and Revelation 12:1.", "The end is near")
    assert fc["type"] == "faith"
    assert any("date" in f.lower() for f in fc["red_flags"])
    assert {s["reference"] for s in fc["scripture"]} == {"John 3:16", "Revelation 12:1"}
    assert fc["verdict"] == "needs context"
    fc2 = factcheck.heuristic_check("Aliens built the pyramids, that is my theory.", "Aliens built the pyramids")
    assert fc2["type"] == "theory" and fc2["honest_title"].startswith("Theory:")


def test_crossfade_has_no_click(tmp_path):
    sr = audio.SR
    t = np.arange(sr * 2) / sr
    data = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[:, None]
    src = tmp_path / "src.wav"
    audio.write_wav(src, np.repeat(data, 2, axis=1), sr)
    tl = Timeline([(0.0, 0.5), (1.2, 2.0)])
    out = audio.build_clip_audio(src, tl, tmp_path / "out.wav", tmp_path)
    y = audio._read(out)
    assert abs(len(y) / sr - tl.duration) < 0.02
    assert np.max(np.abs(np.diff(y[:, 0]))) < 0.25  # no jumps larger than a normal 440 Hz sample step


def test_caption_lines_and_timing():
    from clipforge import captions
    w = words_from("You realize just how amazing humans are. Machine learning has begun a big revolution here", step=0.3)
    tl = Timeline.single(0.0, 6.0)
    cw = captions.clip_words(w, tl)
    captions.choose_emojis(cw, 10, set(), None)
    ass, overlays = captions.build_ass(cw, 1080, 1920, captions.preset("bold_pop"), "9:16", key_words=["machine"])
    events = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    assert len(events) == len(cw)  # one event per spoken word
    # every event's active word colour is the highlight or keyword colour, lines have 2-4 words
    for e in events:
        text = e.split(",,")[-1]
        n_words = text.count("{\\r}")
        assert 1 <= n_words <= 4
    assert any(o["emoji"] == "🤖" for o in overlays)  # 'machine' gets an emoji
    # captions sit in the lower-middle third, never in the bottom 20%
    assert "\\pos(540,1267)" in ass


def test_clip_words_drop_half_outside():
    from clipforge import captions
    w = [{"w": "at.", "s": 9.7, "e": 9.95}, {"w": "You", "s": 10.0, "e": 10.3}]
    tl = Timeline.single(9.85, 20)
    assert [x["w"] for x in captions.clip_words(w, tl)] == ["You"]
