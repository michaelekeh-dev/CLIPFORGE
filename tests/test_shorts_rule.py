"""A Short is vertical AND brief. Judging on shape alone threw away a real episode."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, download  # noqa: E402


@pytest.fixture()
def episodes(monkeypatch):
    db.init_db()
    db.execute("DELETE FROM seen_videos")
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "enabled": True, "min_episode_minutes": 15})
    return monkeypatch


def peek_as(monkeypatch, **info):
    monkeypatch.setattr(download, "peek", lambda url: info)


def test_a_real_short_is_still_refused(episodes):
    peek_as(episodes, duration=58, width=1080, height=1920, vertical=True)
    why = autopilot.episode_problem("https://youtu.be/x")
    assert "58 second vertical" in why and "Short" in why


def test_a_long_vertical_episode_is_not_a_short(episodes):
    """EP.307 was 1080x1920 and got thrown away. Tall does not mean brief."""
    peek_as(episodes, duration=96 * 60, width=1080, height=1920, vertical=True)
    assert autopilot.episode_problem("https://youtu.be/x") == "", "a 96 minute video is an episode, tall or not"


def test_a_vertical_video_right_on_the_line(episodes):
    peek_as(episodes, duration=autopilot.MAX_SHORT_SECONDS, width=1080, height=1920, vertical=True)
    assert "Short" in autopilot.episode_problem("https://youtu.be/x")
    # just over the line it is no longer called a Short — though at 4 minutes it is still refused
    # for being far too brief to be an episode, which is the other rule doing its job
    peek_as(episodes, duration=autopilot.MAX_SHORT_SECONDS + 1, width=1080, height=1920, vertical=True)
    why = autopilot.episode_problem("https://youtu.be/x")
    assert "Short" not in why and "only 4.0 minutes" in why


def test_a_short_landscape_clip_is_still_too_brief_to_be_an_episode(episodes):
    peek_as(episodes, duration=5 * 60, width=1920, height=1080, vertical=False)
    assert "only 5.0 minutes long" in autopilot.episode_problem("https://youtu.be/x")


def test_a_vertical_video_of_unknown_length_is_not_refused(episodes):
    peek_as(episodes, duration=0, width=1080, height=1920, vertical=True)
    assert autopilot.episode_problem("https://youtu.be/x") == "", "do not guess when the length is unknown"


def test_old_shorts_verdicts_are_handed_back_once(episodes):
    db.execute("DELETE FROM settings WHERE key='autopilot_migrations'")
    db.insert("seen_videos", {"video_id": "ep307", "title": "KAI CENAT MAFIATHON THEORY EP.307",
                              "seen_at": time.time(), "project_id": "short"})
    db.insert("seen_videos", {"video_id": "done1", "title": "already clipped", "seen_at": time.time(),
                              "project_id": "p_real"})
    fixed = autopilot.fix_impossible_settings()
    assert any("re-opened" in f for f in fixed)
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='ep307'")["project_id"] == ""
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='done1'")["project_id"] == "p_real", \
        "an episode already clipped must not be re-opened"
    # and never again, so a deliberate skip from here on sticks
    db.execute("UPDATE seen_videos SET project_id='short' WHERE video_id='ep307'")
    autopilot.fix_impossible_settings()
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='ep307'")["project_id"] == "short"
    db.execute("DELETE FROM settings WHERE key='autopilot_migrations'")
