"""Only long-form episodes get clipped. A Short must not survive any route in."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, download, notify, pipeline  # noqa: E402


@pytest.fixture()
def wired(monkeypatch):
    db.init_db()
    db.execute("DELETE FROM seen_videos")
    # a project left mid-flight by an earlier run would legitimately hold backfill off, so park them
    stale = [r["id"] for r in db.rows("SELECT id FROM projects WHERE status IN ('running','queued')")]
    for pid in stale:
        db.update("projects", pid, {"status": "parked-for-test"})
    monkeypatch.setattr(notify, "send_text", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "enabled": True, "min_episode_minutes": 15})
    yield monkeypatch
    for pid in stale:
        db.update("projects", pid, {"status": "queued"})


def test_the_length_rule_is_one_place_and_it_is_strict(wired):
    assert autopilot.too_short_to_clip(30), "a 30 second Short is not an episode"
    assert autopilot.too_short_to_clip(14 * 60), "just under the line is still out"
    assert autopilot.too_short_to_clip(0), "unknown length is not a pass"
    assert autopilot.too_short_to_clip(96 * 60) == ""


def test_an_unknown_length_is_not_called_an_episode_at_the_page_stage(wired):
    """It used to return '' meaning allowed. Now it defers to the measurement after download."""
    monkeypatch = wired
    monkeypatch.setattr(download, "peek", lambda url: {})
    assert autopilot.episode_problem("https://youtu.be/x") == ""
    # ...and the real check refuses it
    assert autopilot.too_short_to_clip(0) != ""


def test_a_zero_duration_no_longer_skips_the_length_rule(wired):
    """`if low and mins and ...` quietly let a 0-length video through."""
    wired.setattr(download, "peek", lambda url: {"duration": 0, "width": 1080, "height": 1920, "vertical": True})
    assert autopilot.episode_problem("https://youtu.be/x") == "", "cannot judge it here"
    assert autopilot.too_short_to_clip(0) != "", "but it is refused where it counts"


def test_a_short_that_reports_its_length_is_refused_before_download(wired):
    wired.setattr(download, "peek", lambda url: {"duration": 30, "width": 1080, "height": 1920, "vertical": True})
    assert "Short" in autopilot.episode_problem("https://youtu.be/x")


def test_a_short_landscape_video_is_refused_on_length(wired):
    wired.setattr(download, "peek", lambda url: {"duration": 8 * 60, "width": 1920, "height": 1080, "vertical": False})
    assert "8.0 minutes" in autopilot.episode_problem("https://youtu.be/x")


def test_rejecting_marks_it_so_the_backlog_never_offers_it_again(wired):
    db.execute("DELETE FROM projects WHERE id='p_short1'")
    db.insert("projects", {"id": "p_short1", "title": "CELEBRITY ONE EYE THEORY", "status": "running",
                           "options": "{}", "info": "{}"})
    db.insert("seen_videos", {"video_id": "v_short1", "title": "CELEBRITY ONE EYE THEORY",
                              "seen_at": time.time(), "project_id": "p_short1"})
    try:
        autopilot.reject_as_short("p_short1", "it is 0.5 minutes long and an episode is at least 15")
        assert db.row("SELECT project_id FROM seen_videos WHERE video_id='v_short1'")["project_id"] == "short"
        assert db.row("SELECT status FROM projects WHERE id='p_short1'")["status"] == "skipped"
        assert all(v["video_id"] != "v_short1" for v in autopilot.backlog())
    finally:
        db.execute("DELETE FROM projects WHERE id='p_short1'")


def test_the_pipeline_stops_a_short_before_the_expensive_work(wired, tmp_path, monkeypatch):
    """The guarantee: even if every earlier check is fooled, the measured file ends it."""
    import subprocess
    src = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
                    str(src)], check=True)
    pid = pipeline.create_project(str(src), {"autopilot": True, "clips": 2})
    reached = []
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **k: reached.append("transcribed") or {})
    try:
        pipeline.run_project(pid, lambda *a, **k: None)
        assert reached == [], "it must stop before transcribing"
        assert db.row("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "skipped"
        assert "3 second" in db.row("SELECT error FROM projects WHERE id=?", (pid,))["error"] or \
               "0.1 minutes" in db.row("SELECT error FROM projects WHERE id=?", (pid,))["error"]
    finally:
        pipeline.delete_project(pid)


def test_a_manual_post_is_not_held_to_the_episode_rule(wired, tmp_path, monkeypatch):
    """You pasting a link yourself is you deciding; the rule is for what autopilot picks."""
    import subprocess
    src = tmp_path / "manual.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
                    str(src)], check=True)
    pid = pipeline.create_project(str(src), {"clips": 1})   # no autopilot flag
    seen = []
    monkeypatch.setattr(pipeline.transcribe, "transcribe",
                        lambda *a, **k: seen.append(1) or (_ for _ in ()).throw(RuntimeError("stop here")))
    try:
        with pytest.raises(RuntimeError):
            pipeline.run_project(pid, lambda *a, **k: None)
        assert seen, "a manual run gets past the episode-length rule"
    finally:
        pipeline.delete_project(pid)


def test_the_backlog_keeps_the_shorts_url_signal(wired, monkeypatch):
    """A backlog row rebuilds the url as watch?v=..., which used to lose the /shorts/ tell."""
    db.insert("seen_videos", {"video_id": "s_x", "title": "no hashtag here", "seen_at": time.time(),
                              "project_id": ""})
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    monkeypatch.setattr(download, "peek", lambda url: {"duration": 45, "width": 1080, "height": 1920, "vertical": True})
    started = []
    monkeypatch.setattr(autopilot, "start_project_from_url", lambda u, title="": started.append(u) or "p_x")
    assert autopilot.backfill_once(force=True) is None
    assert started == [], "a Short in the backlog must not be clipped"
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='s_x'")["project_id"] == "short"
