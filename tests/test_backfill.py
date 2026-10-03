"""When the source channel is quiet, the queue keeps itself topped up from older episodes."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, notify  # noqa: E402


@pytest.fixture()
def clean(monkeypatch):
    db.init_db()
    db.execute("DELETE FROM seen_videos")
    db.execute("DELETE FROM posts")
    db.execute("DELETE FROM projects WHERE id LIKE 'p_bf%'")
    # a project left mid-flight by an earlier run would legitimately hold backfill off, so park them
    stale = [r["id"] for r in db.rows("SELECT id FROM projects WHERE status IN ('running','queued')")]
    for pid in stale:
        db.update("projects", pid, {"status": "parked-for-test"})
    monkeypatch.setattr(notify, "send_text", lambda *a, **k: {"ok": True})
    started = []
    monkeypatch.setattr(autopilot, "start_project_from_url",
                        lambda url, title="": started.append((url, title)) or f"p_bf{len(started)}")
    yield started
    for pid in stale:
        db.update("projects", pid, {"status": "queued"})


def settings(monkeypatch, **over):
    st = {**autopilot.DEFAULTS, "enabled": True, **over}
    monkeypatch.setattr(autopilot, "get_settings", lambda: st)
    return st


def seen(video_id, title, project_id=""):
    db.insert("seen_videos", {"video_id": video_id, "title": title, "seen_at": time.time(), "project_id": project_id})


def queue(n, per_day=2):
    for i in range(n):
        db.insert("posts", {"id": f"post_bf{i}", "clip_id": f"c{i}", "project_id": "p_x", "status": "waiting",
                            "publish_at": time.time() + 3600 * (i + 1), "title": f"t{i}"})


def test_days_queued_counts_in_posting_days(clean, monkeypatch):
    settings(monkeypatch, max_posts_per_day=2)
    queue(6)
    assert autopilot.days_queued() == 3.0


def test_an_empty_queue_pulls_in_an_older_episode(clean, monkeypatch):
    settings(monkeypatch, backfill=True, queue_days=3)
    seen("vid_new", "Episode 50")
    seen("vid_old", "Episode 49")
    pid = autopilot.backfill_once()
    assert pid is not None
    assert clean == [("https://www.youtube.com/watch?v=vid_new", "Episode 50")], "newest unclipped first"
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='vid_new'")["project_id"] == pid


def test_a_full_queue_is_left_alone(clean, monkeypatch):
    settings(monkeypatch, backfill=True, queue_days=3, max_posts_per_day=2)
    seen("vid_old", "Episode 49")
    queue(8)  # 4 days lined up
    assert autopilot.backfill_once() is None
    assert clean == []


def test_backfill_can_be_switched_off(clean, monkeypatch):
    settings(monkeypatch, backfill=False)
    seen("vid_old", "Episode 49")
    assert autopilot.backfill_once() is None
    assert clean == []


def test_nothing_happens_while_an_episode_is_already_being_clipped(clean, monkeypatch):
    settings(monkeypatch, backfill=True)
    seen("vid_old", "Episode 49")
    db.insert("projects", {"id": "p_bf_running", "title": "busy", "status": "running", "options": "{}", "info": "{}"})
    try:
        assert autopilot.backfill_once() is None, "downloads must not pile up on top of each other"
        assert clean == []
    finally:
        db.execute("DELETE FROM projects WHERE id='p_bf_running'")


def test_already_clipped_episodes_are_never_redone(clean, monkeypatch):
    settings(monkeypatch, backfill=True)
    seen("vid_done", "Episode 48", project_id="p_old")
    assert autopilot.backlog() == []
    assert autopilot.backfill_once() is None


def test_shorts_in_the_backlog_are_skipped(clean, monkeypatch):
    settings(monkeypatch, backfill=True)
    seen("vid_short", "Quick one #shorts")
    seen("vid_real", "Episode 47")
    autopilot.backfill_once()
    assert clean == [("https://www.youtube.com/watch?v=vid_real", "Episode 47")]


def test_an_empty_backlog_is_not_an_error(clean, monkeypatch):
    settings(monkeypatch, backfill=True)
    assert autopilot.backfill_once() is None


def test_the_watcher_can_actually_record_what_it_saw(clean):
    """db.insert() stamps created_at on every row, and seen_videos had no such column — so every
    attempt to remember an episode threw, and the channel watcher never got past the first one."""
    db.insert("seen_videos", {"video_id": "vid_rec", "title": "Episode 51", "seen_at": time.time(), "project_id": ""})
    assert db.row("SELECT title FROM seen_videos WHERE video_id='vid_rec'")["title"] == "Episode 51"


def test_check_channel_once_records_every_episode_it_looks_at(clean, monkeypatch):
    settings(monkeypatch, backfill=True)
    feed = [{"id": f"v{i}", "title": f"Episode {60 - i}", "url": f"https://youtu.be/v{i}", "published": "2026-09-2{i}"}
            for i in range(4)]
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: feed)
    started = autopilot.check_channel_once()
    assert len(started) == 1, "only the newest is clipped on the first look"
    # the rest must be remembered, or they can never be used as backlog later
    assert len(autopilot.backlog()) == 3
    assert [v["video_id"] for v in autopilot.backlog()] == ["v1", "v2", "v3"]


def test_a_check_interval_of_days_is_repaired_on_startup(clean):
    """10000 minutes is a week between checks. Nobody means that, so it is put back to hourly."""
    autopilot.save_settings({"check_minutes": 10000})
    fixed = autopilot.fix_impossible_settings()
    assert any("10000" in f for f in fixed), fixed
    assert autopilot.get_settings()["check_minutes"] == 60


def test_a_sensible_interval_is_left_alone(clean):
    autopilot.save_settings({"check_minutes": 360})
    assert autopilot.fix_impossible_settings() == []
    assert autopilot.get_settings()["check_minutes"] == 360


def test_the_form_cannot_take_an_impossible_interval_either(clean):
    from fastapi.testclient import TestClient
    from clipforge.web.app import app
    autopilot.save_settings({"check_minutes": 60})
    TestClient(app).post("/api/autopilot", data={
        "enabled": "on", "channel_url": "https://www.youtube.com/@JumpersJump", "check_minutes": "10000",
        "clips": "10", "length": "medium", "keywords": "God", "mode": "auto", "post_times": "11:00, 18:00",
        "timezone": "Europe/London", "max_posts_per_day": "2", "description_footer": "", "public": "on",
        "backfill": "on", "queue_days": "3"}, follow_redirects=False)
    assert autopilot.get_settings()["check_minutes"] == autopilot.MAX_CHECK_MINUTES
    autopilot.save_settings({"check_minutes": 60})


def test_clips_per_episode_is_raised_once_off_the_old_cap(clean):
    """The saved 10 was the old form's limit, not a decision. Raised once — and only once."""
    db.execute("DELETE FROM settings WHERE key='autopilot_migrations'")
    autopilot.save_settings({"clips": 10})
    assert any("clips per episode" in f for f in autopilot.fix_impossible_settings())
    assert autopilot.get_settings()["clips"] == 15

    # you then choose something else: startup must leave it alone from now on
    autopilot.save_settings({"clips": 8})
    assert autopilot.fix_impossible_settings() == []
    assert autopilot.get_settings()["clips"] == 8
    db.execute("DELETE FROM settings WHERE key='autopilot_migrations'")
    autopilot.save_settings({"clips": 15})


def test_a_higher_setting_is_never_lowered(clean):
    db.execute("DELETE FROM settings WHERE key='autopilot_migrations'")
    autopilot.save_settings({"clips": 20})
    autopilot.fix_impossible_settings()
    assert autopilot.get_settings()["clips"] == 20
    db.execute("DELETE FROM settings WHERE key='autopilot_migrations'")
    autopilot.save_settings({"clips": 15})


def test_clearing_the_waiting_pile_skips_them_and_frees_the_files(clean, tmp_path):
    """24 clips sitting undecided: skip the lot and give the disk back, without touching the queue."""
    from clipforge.config import PROJECTS
    d = PROJECTS / "p_bf_wait" / "clips"
    d.mkdir(parents=True, exist_ok=True)
    db.execute("DELETE FROM projects WHERE id='p_bf_wait'")
    db.insert("projects", {"id": "p_bf_wait", "title": "ep", "status": "done", "options": "{}", "info": "{}"})
    files = []
    for i in range(3):
        f = d / f"c{i}.mp4"
        f.write_bytes(b"x" * 2048)
        files.append(f)
        db.execute("DELETE FROM clips WHERE id=?", (f"c_bf_w{i}",))
        db.insert("clips", {"id": f"c_bf_w{i}", "project_id": "p_bf_wait", "status": "done", "start": 0, "end": 30,
                            "title": f"t{i}", "path": str(f), "data": "{}", "settings": "{}"})
    # one of them is already queued to post, so it must survive
    db.insert("posts", {"id": "post_bf_keep", "clip_id": "c_bf_w0", "project_id": "p_bf_wait", "status": "waiting",
                        "publish_at": time.time() + 3600, "title": "t0"})
    mine = {"c_bf_w0", "c_bf_w1", "c_bf_w2"}
    try:
        before = {c["id"] for c in autopilot.waiting_clips()}
        assert mine & before == {"c_bf_w1", "c_bf_w2"}, "the queued one is not waiting on a decision"
        autopilot.clear_waiting_clips()
        after = {c["id"] for c in autopilot.waiting_clips()}
        assert not (mine & after), "every clip that was waiting should now be skipped"
        assert files[0].exists(), "a queued clip keeps its file"
        assert not files[1].exists() and not files[2].exists()
    finally:
        from clipforge import pipeline
        pipeline.delete_project("p_bf_wait")


def test_an_empty_backlog_pulls_older_videos_from_the_channel(clean, monkeypatch):
    """What the owner asked for: nothing recorded to fall back on, so go and ask the channel."""
    feed = [{"id": "old1", "title": "EP.300", "url": "https://youtu.be/old1", "published": "2026-08-01"},
            {"id": "old2", "title": "teaser #shorts", "url": "https://youtu.be/old2", "published": "2026-08-02"}]
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: feed)
    monkeypatch.setattr(autopilot, "episode_problem", lambda url, title="": "")
    settings(monkeypatch, backfill=True, queue_days=3)
    assert autopilot.backlog() == []
    pid = autopilot.backfill_once()
    assert pid and clean == [("https://www.youtube.com/watch?v=old1", "EP.300")]
    assert db.row("SELECT project_id FROM seen_videos WHERE video_id='old2'")["project_id"] == "short"


def test_asking_for_an_older_episode_ignores_the_queue_depth(clean, monkeypatch):
    settings(monkeypatch, backfill=True, queue_days=3)
    monkeypatch.setattr(autopilot, "episode_problem", lambda url, title="": "")
    monkeypatch.setattr(autopilot, "channel_feed", lambda url: [])
    db.insert("seen_videos", {"video_id": "oldX", "title": "EP.299", "seen_at": time.time(), "project_id": ""})
    queue(20)  # ten days lined up: normal backfill would refuse
    assert autopilot.backfill_once() is None
    assert autopilot.backfill_once(force=True) is not None, "/older is you asking, so it goes ahead"
