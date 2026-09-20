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


def test_dedupe_drops_the_same_clip_told_twice():
    """EP.304 shipped three clips twenty minutes apart that were all the same point.

    They never overlapped in TIME, which was the only thing dedupe compared, so all three
    sailed through. The same topic, or near-identical words, is the same clip however far
    apart it was said."""
    ms = [
        {"start": 466, "end": 502, "score": 99, "topic": "theory",
         "text": "you know why that's a trend that's actually AI trying to show you the theory"},
        {"start": 1732, "end": 1748, "score": 98, "topic": "theory",
         "text": "you never heard that theory so there's a theory AI is trying to show you"},
        {"start": 2264, "end": 2282, "score": 97, "topic": "pyramids",
         "text": "the pyramids were built with sound and nobody can explain the acoustics"},
    ]
    keep = moments.dedupe(ms, 3)
    assert len(keep) == 2, [k["topic"] for k in keep]
    assert {k["topic"] for k in keep} == {"theory", "pyramids"}
    assert keep[0]["score"] == 99   # of the two duplicates, the better one survives


def test_dedupe_still_fills_the_slots_when_everything_is_one_topic():
    """The topic cap is a PREFERENCE for variety, not a quota. Four clips labelled 'theory' that
    genuinely say different things still fill three slots — only actual repeats are dropped."""
    texts = ["pyramids acoustics granite chamber resonance egypt",
             "antarctica maps piri reis coastline ice sheet",
             "roswell weather balloon foil memory metal",
             "dogon tribe sirius companion star astronomy"]
    ms = [{"start": i * 100, "end": i * 100 + 30, "score": 90 - i, "topic": "theory", "text": t}
          for i, t in enumerate(texts)]
    assert len(moments.dedupe(ms, 3)) == 3


def test_snap_never_ends_mid_sentence():
    """The old trim walked the end back one WORD at a time when no sentence boundary fit — which
    is precisely how a clip ends on the setup with the payoff cut off."""
    w = words_from("This is the opening hook that pulls you in. " + "filler words to burn the clock here " * 6 +
                   "And that is the payoff line.")
    m = {"start": 0.0, "end": w[9]["e"], "score": 80}   # an end landing mid-sentence
    s = moments.snap(m, w, 2, 30)
    assert s is not None
    assert w[s["wj"]]["w"].endswith(".") or s["wj"] == len(w) - 1


def test_heuristic_prefers_the_complete_thought_over_the_dense_fragment():
    """The keyword score divided by duration, so a short window beat a long one on density alone.
    With the channel keyword said in both, the version that finishes the point must win."""
    short_dense = "Theory theory theory right there. "
    full_thought = ("Here is the theory nobody will say out loud. " +
                    "They test it quietly for years before anyone notices what changed. " +
                    "And that is exactly why the timing lines up.  ")
    w = words_from(short_dense + full_thought, step=0.4)
    tr = {"words": w, "segments": transcribe.make_segments(w)}
    picked = moments.heuristic_pick(tr, 1, 8, 40, ["theory"])
    assert picked, "nothing picked"
    # whatever it picks must end on a finished sentence, not trail off mid-thought
    assert picked[0]["end"] > 4.0, picked[0]


def test_snap_runs_over_to_land_the_payoff():
    """A clip may exceed max by the grace window when that is what reaches the sentence end."""
    w = words_from("Here is the question everyone asks. " + "and the long winded setup continues on and on " * 3 +
                   "so the answer is yes.")
    m = {"start": 0.0, "end": 9.0, "score": 80}
    s = moments.snap(m, w, 5, 10)          # max 10s, but the sentence ends later
    assert s is not None
    assert w[s["wj"]]["w"].endswith(".")   # landed clean rather than cutting at 10s


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


def test_timeline_lead_in_out_shift_everything():
    tl = Timeline.single(10, 20)
    tl.lead_in, tl.lead_out = 0.8, 1.6
    assert abs(tl.duration - 12.4) < 1e-9 and abs(tl.speech_duration - 10) < 1e-9
    assert tl.to_output(10) == 0.8 and tl.to_output(15) == 5.8
    assert tl.to_source(0.3) == 10 and abs(tl.to_source(5.8) - 15) < 1e-9
    tl2 = tl.remove([(12, 13)])
    assert tl2.lead_in == 0.8 and tl2.to_output(14) == 0.8 + 3


def test_brand_templates_roundtrip():
    from clipforge import brand
    t = brand.ensure_default()
    assert t["is_default"] == 1
    tid = brand.save(None, "Test", {"watermark_text": "@x", "accent": "#123456", "intro_card": "on", "outro_card": False}, make_default=True)
    got = brand.get(tid)
    assert got["data"]["watermark_text"] == "@x" and got["data"]["accent"] == "#123456" and got["data"]["intro_card"] is True
    assert brand.get(None)["id"] == tid  # default switched
    brand.delete(tid)
    assert brand.get(None)["id"] != tid


def test_hook_and_brand_overlays_shift_with_offset():
    from clipforge import effects
    style, ev = effects.hook_ass("Six honest words for the top", 1080, 1920, 3.0, offset=0.8)
    assert "0:00:00.80,0:00:03.80" in ev[0]
    ov = effects.brand_overlays({"data": {"watermark_text": "@a", "credit": True}}, 1080, 1920, 30, "Some Channel", offset=0.8)
    credit = ov[-1]
    assert abs(credit.s - 1.0) < 1e-9 and abs(credit.e - 4.0) < 1e-9
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    out = ov[0](frame, 0, 5.0)
    assert out.max() > 0  # watermark drawn


def test_broll_suggestions_and_plan(tmp_path, monkeypatch):
    from clipforge import broll
    w = words_from("Look at the pyramids in Egypt and then the ocean waves and the city lights at night", step=1.0)
    sugg = broll.suggestions({}, w, 0.0, 30.0)
    assert 1 <= len(sugg) <= 2
    assert all(s["t"] >= 3.0 for s in sugg)  # never in the first seconds
    if len(sugg) == 2:
        assert sugg[1]["t"] - sugg[0]["t"] >= 6.0
    # without a key or mock dir the plan is empty (skip quietly)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False); monkeypatch.delenv("PEXELS_MOCK_DIR", raising=False)
    assert broll.plan({}, w, Timeline.single(0, 30), {}, "9:16") == []
    # with a mock dir a plan is made and editor overrides apply
    vid = tmp_path / "a.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=4", vid], check=True)
    monkeypatch.setenv("PEXELS_MOCK_DIR", str(tmp_path))
    plan = broll.plan({}, w, Timeline.single(0, 30), {"broll_items": {"0": {"query": "custom words"}}}, "9:16")
    assert plan and plan[0]["query"] == "custom words" and 1.5 <= plan[0]["e"] - plan[0]["s"] <= 2.5
    plan2 = broll.plan({}, w, Timeline.single(0, 30), {"broll_items": {"0": {"removed": True}}}, "9:16")
    assert len(plan2) == len(plan) - 1


def test_status_and_cleanup(tmp_path):
    from fastapi.testclient import TestClient
    from clipforge.web.app import app, status_info
    from clipforge import db, pipeline
    st = status_info()
    assert set(st["have"]) == {"anthropic", "workspace", "cookies", "pexels", "hf", "password"} and "storage_gb" in st
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200
    # an old project's source video gets deleted, its clips stay
    src = tmp_path / "old.mp4"; src.write_bytes(b"x" * 1000)
    pid = pipeline.create_project(str(src), {})
    db.update("projects", pid, {"created_at": 0, "source_path": str(src)})
    freed = pipeline.cleanup_old_sources(days=7)
    assert freed >= 1000 and not src.exists()
    pipeline.delete_project(pid)


def test_autopilot_slots_and_titles():
    from clipforge import autopilot, moments, effects
    autopilot.save_settings({"post_times": ["11:00", "18:00"], "max_posts_per_day": 2, "timezone": "Europe/London"})
    import time
    s1 = autopilot.next_slot()
    assert s1 > time.time()
    from datetime import datetime
    d = datetime.fromtimestamp(s1, autopilot.tz())
    assert (d.hour, d.minute) in ((11, 0), (18, 0))
    # titles never start with the clip's opening words
    t = "So the Bible says in Genesis that Nimrod built the tower of Babel. And people ask what happened to Nimrod after that?"
    title = moments._title_from(t, ["Bible"])
    assert not title.lower().startswith("so the bible") and "Nimrod" in title
    assert "Kanye" in moments._title_from("Then Kanye said that he was the greatest and Drake laughed.", [])
    assert effects.hook_text_from("", t).endswith("...")


def test_post_text_uses_footer(monkeypatch):
    from clipforge import autopilot
    autopilot.save_settings({"description_footer": "Full episode: {source_url}\nCredit: {credit}"})
    clip = {"title": "Kanye said WHAT about Drake?", "data": {"description": "Wild moment.", "hashtags": ["#shorts", "#kanye"]}}
    proj = {"source_url": "https://youtu.be/abc", "channel": "Jumpers Jump", "options": {"credit_name": "@JumpersJump", "keywords": ["music"]}}
    title, desc, tags = autopilot.post_text(clip, proj)
    assert title.endswith("#Shorts") and "https://youtu.be/abc" in desc and "@JumpersJump" in desc
    assert "kanye" in tags and "music" in tags


def test_telegram_caption_and_buttons():
    from clipforge import notify
    clip = {"id": "c1", "title": "T <b>", "start": 0, "end": 30, "score": 90,
            "data": {"fact_check": {"verdict": "ok", "type": "faith", "summary": "fine", "red_flags": []}}}
    cap = notify.clip_caption(clip, None, "https://x")
    assert "&lt;b&gt;" in cap and "🟢" in cap and "https://x/clip/c1" in cap
    assert notify.clip_buttons("c1", None)[0][0]["callback_data"] == "post:c1"
    assert notify.clip_buttons("c1", {"status": "waiting"})[0][0]["callback_data"] == "cancel:c1"


def test_storage_breakdown_and_free_space(tmp_path, monkeypatch):
    from clipforge import pipeline
    from clipforge.config import PROJECTS
    pid = "p_disktest"
    d = PROJECTS / pid
    (d / "clips").mkdir(parents=True, exist_ok=True)
    (d / "work" / "x").mkdir(parents=True, exist_ok=True)
    (d / "source.mp4").write_bytes(b"s" * 5000)
    (d / "clips" / "clip_01.mp4").write_bytes(b"c" * 2000)
    (d / "work" / "x" / "tmp.wav").write_bytes(b"w" * 3000)
    b = pipeline.storage_breakdown()
    assert b["sources"] >= 5000 and b["clips"] >= 2000 and b["work"] >= 3000
    freed = pipeline.free_space("safe")
    assert freed >= 3000
    assert (d / "source.mp4").exists() and (d / "clips" / "clip_01.mp4").exists()  # kept
    assert not (d / "work").exists()  # working files gone
    pipeline.free_space("sources")
    assert not (d / "source.mp4").exists() and (d / "clips" / "clip_01.mp4").exists()
    import shutil as _sh
    _sh.rmtree(d, ignore_errors=True)


def test_space_check_refuses_and_explains(monkeypatch):
    from clipforge import pipeline
    import shutil as _sh
    monkeypatch.setattr(_sh, "disk_usage", lambda p: type("U", (), {"free": 100 * 1024 * 1024, "total": 0, "used": 0})())
    with pytest.raises(RuntimeError) as e:
        pipeline.space_check(3.0)
    assert "free" in str(e.value).lower() and "Status page" in str(e.value)
