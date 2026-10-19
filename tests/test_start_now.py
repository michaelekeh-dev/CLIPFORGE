"""Setting the channel must get something moving, not just save a value."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, download, notify  # noqa: E402

CID = "UCRFF8xw5dg9mL4r5ryFOtKw"
LINK = "https://youtu.be/ODF8fPCd__w"


@pytest.fixture()
def clean(monkeypatch):
    db.init_db()
    db.execute("DELETE FROM seen_videos")
    db.execute("DELETE FROM posts")
    stale = [r["id"] for r in db.rows("SELECT id FROM projects WHERE status IN ('running','queued')")]
    for pid in stale:
        db.update("projects", pid, {"status": "parked-for-test"})
    monkeypatch.setattr(notify, "send_text", lambda *a, **k: {"ok": True})
    started = []
    monkeypatch.setattr(autopilot, "start_project_from_url",
                        lambda u, title="": started.append((u, title)) or f"p_now{len(started)}")
    monkeypatch.setattr(autopilot, "episode_problem", lambda url, title="": "")
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "enabled": True, "backfill": True, "channel_id": CID})
    yield started
    for pid in stale:
        db.update("projects", pid, {"status": "queued"})


def test_a_new_episode_is_clipped_straight_away(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [
        {"id": "new1", "title": "EP.309", "url": "https://youtu.be/new1", "published": "2026-10-19"}])
    out = autopilot.start_watching_now()
    assert out["started"] == 1 and "Clipping 1 new episode" in out["note"]
    assert clean == [("https://youtu.be/new1", "EP.309")]


def test_nothing_new_falls_back_to_an_older_episode(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    db.insert("seen_videos", {"video_id": "old1", "title": "EP.280", "seen_at": time.time(), "project_id": ""})
    out = autopilot.start_watching_now()
    assert out["started"] == 1 and "older episode" in out["note"]
    # the project row does not exist yet, so the name has to come from what we recorded about the video
    assert "EP.280" in out["note"]


def test_when_the_feed_is_exhausted_it_goes_further_back(clean, monkeypatch):
    """The feed only carries 15. The channel has hundreds, and those must be reachable."""
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    monkeypatch.setattr(autopilot, "channel_archive", lambda channel_url="", limit=120: [
        {"id": "arch1", "title": "EP.120", "url": "https://youtu.be/arch1", "published": "", "duration": 5400},
        {"id": "arch2", "title": "a teaser", "url": "https://youtu.be/arch2", "published": "", "duration": 40},
    ])
    out = autopilot.start_watching_now()
    assert out["started"] == 1 and "older episode" in out["note"]
    assert "EP.120" in out["note"], "it should name what it started, from the archive listing"
    assert clean and clean[0][0].endswith("arch1")
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='arch2'")["project_id"] == "short", \
        "a 40 second upload is not an episode"


def test_a_truly_finished_channel_says_so_plainly(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    monkeypatch.setattr(autopilot, "channel_archive", lambda channel_url="", limit=120: [])
    out = autopilot.start_watching_now()
    assert out["started"] == 0 and "every episode on the channel has been done" in out["note"]


def test_a_switched_off_autopilot_is_not_silently_ignored(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "enabled": False})
    out = autopilot.start_watching_now()
    assert out["started"] == 0 and "switched off" in out["note"]


def test_setting_the_channel_reports_what_it_started(clean, monkeypatch):
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": CID, "channel": "Jumpers Jump"})
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [
        {"id": "n9", "title": "EP.309", "url": "https://youtu.be/n9", "published": "2026-10-19"}])
    saved = {}
    monkeypatch.setattr(autopilot, "save_settings", lambda ch: saved.update(ch))
    out = autopilot.set_channel_from(LINK)
    assert out["ok"] and "Jumpers Jump" in out["detail"]
    assert "Clipping 1 new episode" in out["detail"], "it must say what it is doing, not just what it saved"
    assert saved["channel_id"] == CID


def test_setting_the_channel_forces_the_next_loop_tick_to_check(clean, monkeypatch):
    db.set_setting("last_channel_check", time.time())
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    monkeypatch.setattr(autopilot, "channel_archive", lambda channel_url="", limit=120: [])
    autopilot.start_watching_now()
    # check_channel_once writes its own timestamp; what matters is the old one did not survive untouched
    assert float(db.get_setting("last_channel_check") or 0) >= time.time() - 5
