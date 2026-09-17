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


def test_spring_settles_without_overshoot():
    from clipforge.reframe import Spring
    sp = Spring(0.0, 0.5)
    xs = [sp.step(100.0, 1 / 30) for _ in range(90)]
    assert max(xs) <= 100.0 + 1e-6 and abs(xs[-1] - 100) < 2


def test_choose_layout():
    from clipforge.reframe import choose_layout
    big = {"id": 0, "cx": 300, "cy": 300, "h": 200, "size": 0.28, "coverage": 1}
    big2 = {"id": 1, "cx": 900, "cy": 300, "h": 190, "size": 0.26, "coverage": 1}
    tiny = {"id": 2, "cx": 900, "cy": 300, "h": 50, "size": 0.07, "coverage": 1}
    assert choose_layout([big], 1280, 720) == "single"
    assert choose_layout([big, big2], 1280, 720) == "split"
    assert choose_layout([big, tiny], 1280, 720) == "single"
    assert choose_layout([], 1280, 720) == "wide"
    assert choose_layout([big, big2, tiny], 1280, 720) in ("wide", "speaker")


def test_speaker_turns_switch_at_most_every_2s():
    from clipforge.reframe import speaker_turns
    # two faces; face 0 moves its lips for 0-4s, face 1 for 4-8s, 5 samples per second
    def pts(active_from, active_to):
        out = []
        for i in range(40):
            t = i / 5
            m = (0.1 if i % 2 else 0.4) if active_from <= t < active_to else 0.1
            out.append([t, 100, 100, 50, 60, m])
        return out
    tracks = [{"id": 0, "pts": pts(0, 4)}, {"id": 1, "pts": pts(4, 8)}]
    turns = speaker_turns(tracks, 0.0, 8.0, None)
    ids = [t[2] for t in turns]
    assert ids[0] == 0 and ids[-1] == 1
    assert all(b - a >= 2.0 - 1e-6 for a, b, _, _ in turns[:-1])


def test_filler_cuts_never_touch_kept_words():
    from clipforge import filler
    w = words_from("So um I I think the the answer is yes", step=0.5)
    # add a long silence before the last word
    w[-1]["s"] += 2.0; w[-1]["e"] += 2.0
    cuts = filler.find_cuts(w, "light", None, start=0, end=10)
    whys = " ".join(c["why"] for c in cuts)
    assert "filler: um" in whys and "false start: I" in whys and "silence" in whys
    kept = [x for x in w if not any(c["s"] <= x["s"] and x["e"] <= c["e"] for c in cuts)]
    for x in kept:
        for c in cuts:
            assert c["e"] <= x["s"] + 1e-6 or c["s"] >= x["e"] - 1e-6  # never mid-word
    tl = Timeline.single(0, 10).remove([(c["s"], c["e"]) for c in cuts])
    assert tl.duration < 10


def test_zoom_windows_avoid_cuts():
    from clipforge import effects
    tl = Timeline.single(10, 30)
    shots = [{"start": 10, "end": 15}, {"start": 15, "end": 30}]
    z = effects.zoom_windows([14.9, 20.0], tl, shots, 1.12)
    assert len(z) >= 1
    for w in z:
        s_src, e_src = tl.to_source(w["s"]), tl.to_source(w["e"])
        assert not (s_src < 15 < e_src)  # never across the cut
    fn = effects.ZoomFn(z)
    assert fn(z[0]["peak_s"] + 0.1) == 1.12 and fn(z[0]["e"] + 0.5) == 1.0
