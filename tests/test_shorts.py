"""Autopilot must never clip the channel's own Shorts — a Short is already a finished vertical video."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, download, notify  # noqa: E402


@pytest.fixture()
def quiet(monkeypatch):
    db.init_db()
    db.execute("DELETE FROM seen_videos")
    db.execute("DELETE FROM posts")
    # a project left mid-flight by an earlier run would legitimately hold backfill off, so park them
    stale = [r["id"] for r in db.rows("SELECT id FROM projects WHERE status IN ('running','queued')")]
    for pid in stale:
        db.update("projects", pid, {"status": "parked-for-test"})
    monkeypatch.setattr(notify, "send_text", lambda *a, **k: {"ok": True})
    started = []
    monkeypatch.setattr(autopilot, "start_project_from_url",
                        lambda url, title="": started.append(url) or f"p_s{len(started)}")
    yield started
    for pid in stale:
        db.update("projects", pid, {"status": "queued"})


def peek_as(monkeypatch, **info):
    monkeypatch.setattr(download, "peek", lambda url: info)


def test_a_shorts_link_is_spotted_from_the_url_alone():
    assert autopilot._looks_like_short({"title": "EP.305", "url": "https://www.youtube.com/shorts/abc123"})


def test_a_shorts_hashtag_is_spotted():
    assert autopilot._looks_like_short({"title": "wild moment #Shorts", "url": "https://youtu.be/x"})


def test_a_normal_episode_is_not_mistaken_for_one():
    assert not autopilot._looks_like_short({"title": "HE CAN CONTROL THE CLOUDS? EP.305", "url": "https://youtu.be/x"})


def test_a_vertical_video_is_refused_however_it_is_titled(quiet, monkeypatch):
    """The one that got through: a Short whose title says nothing and whose URL is a normal watch link."""
    peek_as(monkeypatch, duration=58, width=1080, height=1920, vertical=True)
    why = autopilot.episode_problem("https://youtu.be/x", "HE CAN CONTROL THE CLOUDS?")
    assert "vertical" in why and "Short" in why


def test_a_video_shorter_than_an_episode_is_refused(quiet, monkeypatch):
    peek_as(monkeypatch, duration=4 * 60, width=1920, height=1080, vertical=False)
    why = autopilot.episode_problem("https://youtu.be/x")
    assert "4 minutes" in why


def test_a_real_episode_passes(quiet, monkeypatch):
    peek_as(monkeypatch, duration=95 * 60, width=1920, height=1080, vertical=False)
    assert autopilot.episode_problem("https://youtu.be/x") == ""


def test_when_youtube_will_not_say_we_do_not_block_the_episode(quiet, monkeypatch):
    peek_as(monkeypatch)  # {} — could not read it
    assert autopilot.episode_problem("https://youtu.be/x") == ""


def test_the_watcher_skips_shorts_and_clips_the_episode(quiet, monkeypatch):
    feed = [
        {"id": "s1", "title": "clip of the week", "url": "https://www.youtube.com/shorts/s1", "published": "2026-10-01"},
        {"id": "e1", "title": "EP.305 full episode", "url": "https://youtu.be/e1", "published": "2026-09-30"},
    ]
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: feed)
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "enabled": True})
    peek_as(monkeypatch, duration=95 * 60, width=1920, height=1080, vertical=False)
    autopilot.check_channel_once()
    assert quiet == ["https://youtu.be/e1"], "only the real episode should be clipped"
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='s1'")["project_id"] == "short"


def test_a_short_never_comes_back_through_the_backlog(quiet, monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "enabled": True, "backfill": True, "queue_days": 3})
    db.insert("seen_videos", {"video_id": "v_short", "title": "nothing suspicious", "seen_at": time.time(),
                              "project_id": ""})
    peek_as(monkeypatch, duration=45, width=1080, height=1920, vertical=True)
    assert autopilot.backfill_once() is None
    assert quiet == [], "a vertical video must not be clipped from the backlog either"
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='v_short'")["project_id"] == "short"
    assert autopilot.backlog() == []


def test_the_first_look_clips_the_newest_episode_even_if_a_short_is_newer(quiet, monkeypatch):
    """If the newest thing on the channel is a Short, the real episode underneath it must still be
    the one that gets clipped — not marked as old news and skipped forever."""
    feed = [
        {"id": "s9", "title": "teaser", "url": "https://www.youtube.com/shorts/s9", "published": "2026-10-01"},
        {"id": "e9", "title": "EP.306 full episode", "url": "https://youtu.be/e9", "published": "2026-09-30"},
        {"id": "e8", "title": "EP.305 full episode", "url": "https://youtu.be/e8", "published": "2026-09-23"},
    ]
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: feed)
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "enabled": True})
    peek_as(monkeypatch, duration=95 * 60, width=1920, height=1080, vertical=False)
    autopilot.check_channel_once()
    assert quiet == ["https://youtu.be/e9"]
    assert [v["video_id"] for v in autopilot.backlog()] == ["e8"], "the older episode stays available"
