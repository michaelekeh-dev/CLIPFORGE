"""When nothing is arriving, the app must say why rather than going quiet."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, notify, youtube, llm  # noqa: E402


@pytest.fixture()
def wired(monkeypatch):
    """Everything connected, so why_quiet talks about the queue rather than the setup."""
    db.init_db()
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM seen_videos")
    db.execute("DELETE FROM errors")
    db.execute("DELETE FROM settings WHERE key IN ('last_heartbeat','last_channel_check')")
    sent = []
    monkeypatch.setattr(notify, "send_text", lambda text, *a, **k: sent.append(text) or {"ok": True})
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "chat_id", lambda: "42")
    monkeypatch.setattr(youtube, "connected", lambda: True)
    monkeypatch.setattr(llm, "mode", lambda: "live")
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "enabled": True, "mode": "auto", "check_minutes": 60})
    return sent


def test_a_switched_off_autopilot_says_so(monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "enabled": False})
    assert "switched off" in autopilot.why_quiet()


def test_a_missing_connection_is_named(monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "enabled": True})
    monkeypatch.setattr(youtube, "connected", lambda: False)
    why = autopilot.why_quiet()
    assert "cannot run on its own yet" in why and "YouTube" in why


def test_a_watcher_that_never_ran_is_the_headline(wired):
    why = autopilot.why_quiet()
    assert "never been checked" in why
    assert "may not have restarted" in why


def test_a_stuck_watcher_is_reported_with_how_long(wired):
    db.set_setting("last_channel_check", time.time() - 20 * 3600)
    why = autopilot.why_quiet()
    assert "20 hours ago" in why and "looks stuck" in why


def test_a_healthy_queue_explains_the_silence_as_deliberate(wired):
    db.set_setting("last_channel_check", time.time() - 600)
    for i in range(6):
        db.insert("posts", {"id": f"post_hb{i}", "clip_id": f"c{i}", "project_id": "p", "status": "waiting",
                            "publish_at": time.time() + 3600 * (i + 1), "title": "t"})
    why = autopilot.why_quiet()
    assert "already lined up" in why and "on purpose" in why


def test_an_empty_queue_with_no_backlog_is_explained(wired):
    db.set_setting("last_channel_check", time.time() - 600)
    assert "nothing to post until the channel puts out something new" in autopilot.why_quiet()


def test_the_last_error_is_included(wired):
    db.set_setting("last_channel_check", time.time() - 600)
    db.log_error("autopilot", "feed: Unable to download API page\nsecond line")
    why = autopilot.why_quiet()
    assert "Unable to download API page" in why
    assert "second line" not in why, "one line is enough"


def quiet_since(monkeypatch, hours):
    """Pretend the last clip landed `hours` ago, whatever else is in the database."""
    t = time.time() - hours * 3600
    monkeypatch.setattr(autopilot, "last_activity",
                        lambda: {"channel_check": time.time() - 600, "clip_made": t, "posted": t,
                                 "project_started": t})


def test_the_heartbeat_stays_quiet_when_clips_are_flowing(wired, monkeypatch):
    quiet_since(monkeypatch, 2)
    assert autopilot.heartbeat() == ""
    assert wired == [], "do not message when it is working"


def test_the_heartbeat_speaks_up_after_a_day_of_nothing(wired, monkeypatch):
    db.set_setting("last_channel_check", time.time() - 600)
    quiet_since(monkeypatch, 30)
    out = autopilot.heartbeat()
    assert out and len(wired) == 1
    assert "Nothing posted in the last day" in wired[0]


def test_the_heartbeat_does_not_nag_more_than_once_a_day(wired, monkeypatch):
    db.set_setting("last_channel_check", time.time() - 600)
    quiet_since(monkeypatch, 30)
    autopilot.heartbeat()
    assert len(wired) == 1
    autopilot.heartbeat()
    autopilot.heartbeat()
    assert len(wired) == 1, "one message a day, not one a minute"


def test_the_heartbeat_can_be_switched_off(wired, monkeypatch):
    from clipforge import config
    monkeypatch.setattr(autopilot.cfg, "get", lambda k, d=None: False if k == "app.heartbeat" else d)
    assert autopilot.heartbeat() == ""
    assert wired == []


def test_the_why_command_answers_on_telegram(wired):
    notify.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/why"}}, "https://app.test")
    assert wired and ("never been checked" in wired[-1] or "lined up" in wired[-1])


def test_the_why_command_says_so_when_all_is_well(wired, monkeypatch):
    monkeypatch.setattr(autopilot, "why_quiet", lambda: "")
    notify.handle_update({"update_id": 2, "message": {"chat": {"id": 42}, "text": "/why"}}, "https://app.test")
    assert "Nothing wrong" in wired[-1]


def test_a_manual_channel_check_records_the_time(wired, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    db.execute("DELETE FROM settings WHERE key='last_channel_check'")
    autopilot.check_channel_once()
    assert float(db.get_setting("last_channel_check") or 0) > time.time() - 10
