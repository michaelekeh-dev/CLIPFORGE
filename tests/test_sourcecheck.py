"""A source that already has subtitles burned in is somebody's finished edit, not raw footage."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import sourcecheck  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTIONED = [os.path.join(ROOT, "samples", "1.7", f"clip_{i:02d}.mp4") for i in (1, 2, 3)]
HAVE = all(os.path.exists(p) for p in CAPTIONED)


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    """The same footage with the caption area cropped away: real frames, no burned-in text."""
    if not HAVE:
        pytest.skip("no rendered samples to read back")
    out = tmp_path_factory.mktemp("plain") / "nocaps.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", CAPTIONED[0],
                    "-vf", "crop=iw:ih*0.30:0:0", "-an", str(out)], check=True)
    return str(out)


@pytest.mark.skipif(not HAVE, reason="no rendered samples")
@pytest.mark.parametrize("path", CAPTIONED)
def test_burned_in_captions_are_found(path):
    out = sourcecheck.burned_in_captions(path)
    assert out["captioned"], f"{path} has our captions burned in: {out}"
    assert "already burned into this video" in out["detail"]


def test_footage_without_captions_is_not_flagged(plain):
    out = sourcecheck.burned_in_captions(plain)
    assert not out["captioned"], out


def test_what_separates_them_is_the_text_changing(plain):
    """Bright blobs alone are not captions — a face or a wall gives those. Words change; a logo does not."""
    yes = sourcecheck.burned_in_captions(CAPTIONED[0])
    no = sourcecheck.burned_in_captions(plain)
    assert no["changing"] < 0.2, "static brightness must not read as captions"
    assert yes["changing"] > no["changing"] * 2


@pytest.mark.skipif(not HAVE, reason="no rendered samples")
def test_problem_gives_a_sentence_for_an_edited_source():
    why = sourcecheck.problem(CAPTIONED[0])
    assert why and "subtitles are already burned into this video" in why


def test_problem_is_quiet_for_raw_footage(plain):
    assert sourcecheck.problem(plain) == ""


def test_the_check_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(sourcecheck.cfg, "get",
                        lambda k, d=None: False if k == "source.refuse_edited" else d)
    if HAVE:
        assert sourcecheck.problem(CAPTIONED[0]) == ""


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    bad = tmp_path / "nope.mp4"
    bad.write_bytes(b"not a video")
    out = sourcecheck.burned_in_captions(str(bad))
    assert out["captioned"] is False and out["detail"]


def test_the_pipeline_refuses_an_edited_source(tmp_path, monkeypatch):
    """End to end: an autopilot project whose download turns out to be edited stops before transcribing."""
    from clipforge import db, pipeline, autopilot, notify
    if not HAVE:
        pytest.skip("no rendered samples")
    db.init_db()
    monkeypatch.setattr(notify, "send_text", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(autopilot, "too_short_to_clip", lambda s: "")   # long enough; the edit is the problem
    reached = []
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **k: reached.append(1) or {})
    pid = pipeline.create_project(CAPTIONED[0], {"autopilot": True, "clips": 1})
    try:
        pipeline.run_project(pid, lambda *a, **k: None)
        assert reached == [], "it must stop before transcribing an edited source"
        row = db.row("SELECT status, error FROM projects WHERE id=?", (pid,))
        assert row["status"] == "skipped" and "burned into" in row["error"]
    finally:
        pipeline.delete_project(pid)
