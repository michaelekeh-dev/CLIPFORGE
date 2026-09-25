"""One episode on disk at a time — without ever deleting a clip that has not gone up yet."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, pipeline  # noqa: E402
from clipforge.config import PROJECTS  # noqa: E402


@pytest.fixture()
def episodes():
    db.init_db()
    made = []

    def episode(pid, clips, source_mb=2):
        d = PROJECTS / pid
        (d / "clips").mkdir(parents=True, exist_ok=True)
        (d / "work").mkdir(parents=True, exist_ok=True)
        (d / "source.mp4").write_bytes(b"s" * source_mb * 1024 * 1024)
        (d / "work" / "tmp.bin").write_bytes(b"w" * 1024 * 512)
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
        db.insert("projects", {"id": pid, "title": pid, "status": "done", "options": "{}", "info": "{}"})
        out = []
        for cid, post_status in clips:
            f = d / "clips" / f"{cid}.mp4"
            f.write_bytes(b"c" * 1024 * 256)
            db.execute("DELETE FROM clips WHERE id=?", (cid,))
            db.insert("clips", {"id": cid, "project_id": pid, "status": "done", "start": 0, "end": 30,
                                "title": cid, "path": str(f), "data": "{}", "settings": "{}"})
            db.execute("DELETE FROM posts WHERE clip_id=?", (cid,))
            if post_status:
                db.insert("posts", {"id": "post_" + cid, "clip_id": cid, "project_id": pid,
                                    "status": post_status, "publish_at": time.time() + 3600, "title": cid})
            out.append(f)
        made.append(pid)
        return out

    yield episode
    for pid in made:
        pipeline.delete_project(pid)


def test_an_old_episodes_source_and_working_files_go(episodes):
    files = episodes("p_st_old", [("c_st_1", "uploaded")])
    assert (PROJECTS / "p_st_old" / "source.mp4").exists()
    pipeline.make_room(keep_pid="p_st_new")
    assert not (PROJECTS / "p_st_old" / "source.mp4").exists()
    assert not (PROJECTS / "p_st_old" / "work").exists()
    assert not files[0].exists(), "a clip already on YouTube does not need its file"


def test_a_clip_still_waiting_to_post_is_never_deleted(episodes):
    files = episodes("p_st_queued", [("c_st_wait", "waiting"), ("c_st_up", "uploaded")])
    pipeline.make_room(keep_pid="p_st_other")
    assert files[0].exists(), "deleting a queued clip would break the post that is scheduled for it"
    assert not files[1].exists()


def test_a_clip_you_have_not_decided_on_is_never_deleted(episodes):
    """No post row at all means it is sitting in Telegram waiting for your tap."""
    files = episodes("p_st_undecided", [("c_st_none", None)])
    pipeline.make_room(keep_pid="p_st_other")
    assert files[0].exists()


def test_a_skipped_clip_is_cleared(episodes):
    files = episodes("p_st_skip", [("c_st_skip", "skipped")])
    pipeline.make_room(keep_pid="p_st_other")
    assert not files[0].exists()


def test_the_episode_being_worked_on_is_left_alone(episodes):
    files = episodes("p_st_current", [("c_st_cur", "uploaded")])
    pipeline.make_room(keep_pid="p_st_current")
    assert (PROJECTS / "p_st_current" / "source.mp4").exists(), "never delete the episode being clipped"
    assert files[0].exists()


def test_make_room_reports_what_it_freed(episodes):
    episodes("p_st_report", [("c_st_rep", "uploaded")], source_mb=3)
    out = pipeline.make_room(keep_pid="p_st_none")
    assert out["sources"] >= 3 * 1024 * 1024
    assert out["freed"] > out["sources"]
    assert out["kept_clips"] >= 0


def test_dropping_a_finished_episodes_source_keeps_its_clips(episodes):
    files = episodes("p_st_drop", [("c_st_d", "waiting")])
    n = pipeline.drop_source("p_st_drop")
    assert n > 0 and not (PROJECTS / "p_st_drop" / "source.mp4").exists()
    assert files[0].exists(), "the clips are the whole point; only the download goes"
    assert db.row("SELECT source_path FROM projects WHERE id='p_st_drop'")["source_path"] == ""


def test_pending_ids_cover_every_undecided_and_queued_clip(episodes):
    episodes("p_st_pend", [("c_st_a", "waiting"), ("c_st_b", None), ("c_st_c", "uploaded"), ("c_st_d", "error")])
    pending = pipeline.pending_clip_ids()
    assert {"c_st_a", "c_st_b", "c_st_d"} <= pending
    assert "c_st_c" not in pending


def test_old_undecided_clips_are_cleared_only_when_asked(episodes):
    """The disk fills with clips you never tapped. Normal cleanup protects them; the escalation
    that runs when a download still will not fit lets the old ones go."""
    files = episodes("p_st_stale", [("c_st_stale", None)])
    db.update("projects", "p_st_stale", {"created_at": time.time() - 40 * 86400})

    pipeline.make_room(keep_pid="p_st_other")
    assert files[0].exists(), "not while there is still room"

    pipeline.make_room(keep_pid="p_st_other", stale_days=14)
    assert not files[0].exists()


def test_the_escalation_still_will_not_touch_a_queued_clip(episodes):
    files = episodes("p_st_stale2", [("c_st_q", "waiting"), ("c_st_n", None)])
    db.update("projects", "p_st_stale2", {"created_at": time.time() - 90 * 86400})
    pipeline.make_room(keep_pid="p_st_other", stale_days=1)
    assert files[0].exists(), "a scheduled post must never lose its file, however old the episode is"
    assert not files[1].exists()


def test_recent_undecided_clips_survive_the_escalation(episodes):
    files = episodes("p_st_recent", [("c_st_r", None)])
    db.update("projects", "p_st_recent", {"created_at": time.time() - 2 * 86400})
    pipeline.make_room(keep_pid="p_st_other", stale_days=14)
    assert files[0].exists(), "you have not had a chance to look at these yet"


def test_the_status_page_buttons_do_what_they_say(episodes):
    from fastapi.testclient import TestClient
    from clipforge.web.app import app
    files = episodes("p_st_btn", [("c_st_btn_up", "uploaded"), ("c_st_btn_wait", "waiting")])
    r = TestClient(app).post("/api/cleanup", data={"kind": "room"}, follow_redirects=False)
    assert r.status_code == 303 and "Freed" in r.headers["location"]
    assert not files[0].exists() and files[1].exists()


def test_cleanup_never_follows_a_path_out_of_the_projects_folder(tmp_path):
    """A clip row can hold any path at all. Cleanup deleted whatever it found there, and during
    development that deleted a source file in the repo. It must refuse anything outside our folder."""
    outsider = tmp_path / "precious.txt"
    outsider.write_text("do not delete me")
    db.execute("DELETE FROM clips WHERE id='c_st_outside'")
    db.insert("clips", {"id": "c_st_outside", "project_id": "p_st_gone", "status": "done", "start": 0, "end": 5,
                        "title": "stray", "path": str(outsider), "data": "{}", "settings": "{}"})
    try:
        assert pipeline.inside_projects(outsider) is False
        assert pipeline._unlink_ours(outsider) == 0
        pipeline.make_room(keep_pid=None, stale_days=0)
        assert outsider.exists(), "cleanup must never delete a file outside the projects folder"
        assert outsider.read_text() == "do not delete me"
    finally:
        db.execute("DELETE FROM clips WHERE id='c_st_outside'")


def test_a_real_project_file_is_still_inside(episodes):
    files = episodes("p_st_in", [("c_st_in", "uploaded")])
    assert pipeline.inside_projects(files[0]) is True
