"""Every clip card says what it was cut from, so a Short can never masquerade as an episode."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, notify, pipeline  # noqa: E402
from clipforge.config import PROJECTS  # noqa: E402


@pytest.fixture()
def made(monkeypatch):
    db.init_db()
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "min_episode_minutes": 15})
    monkeypatch.setattr(notify, "send_text", lambda *a, **k: {"ok": True})
    ids = []

    def make(pid, minutes, cid):
        d = PROJECTS / pid / "clips"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{cid}.mp4"
        f.write_bytes(b"x" * 1024)
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
        db.insert("projects", {"id": pid, "title": f"source {pid}", "status": "done", "options": "{}",
                               "info": "{}", "duration": minutes * 60})
        db.execute("DELETE FROM clips WHERE id=?", (cid,))
        db.insert("clips", {"id": cid, "project_id": pid, "status": "done", "start": 0, "end": 30, "score": 70,
                            "title": "a clip", "path": str(f), "data": json.dumps({"text": "hi"}), "settings": "{}"})
        ids.append(pid)
        return f

    yield make
    for pid in ids:
        pipeline.delete_project(pid)


def caption_for(cid):
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", (cid,)), "data", "settings")
    return notify.clip_caption(clip, None, "https://app.test")


def test_a_clip_from_a_real_episode_shows_the_source_length(made):
    made("p_prov_ep", 96, "c_prov_ep")
    cap = caption_for("c_prov_ep")
    assert "🎬 from source p_prov_ep · 96 min source" in cap


def test_a_clip_cut_from_a_short_says_so_on_its_face(made):
    made("p_prov_short", 0.5, "c_prov_short")
    cap = caption_for("c_prov_short")
    assert "⚠️" in cap and "30s source — NOT an episode" in cap


def test_the_purge_removes_short_sourced_clips_and_keeps_real_ones(made):
    short_f = made("p_prov_s2", 0.8, "c_prov_s2")
    ep_f = made("p_prov_e2", 90, "c_prov_e2")
    removed, kept = autopilot.purge_short_sourced_clips()
    assert removed >= 1 and kept >= 1
    assert not short_f.exists(), "a clip cut from a Short should be gone"
    assert ep_f.exists(), "a clip from a real episode must survive"
    assert db.row("SELECT status FROM posts WHERE clip_id='c_prov_s2'")["status"] == "skipped"
    assert not db.row("SELECT 1 FROM posts WHERE clip_id='c_prov_e2'")


def test_an_unknown_source_length_is_left_alone(made):
    f = made("p_prov_unk", 0, "c_prov_unk")
    autopilot.purge_short_sourced_clips()
    assert f.exists(), "never throw away a clip just because the source length was not recorded"


def test_a_clip_with_no_project_still_renders_a_caption(made):
    db.execute("DELETE FROM clips WHERE id='c_prov_orphan'")
    db.insert("clips", {"id": "c_prov_orphan", "project_id": "p_gone", "status": "done", "start": 0, "end": 30,
                        "score": 50, "title": "orphan", "path": "", "data": "{}", "settings": "{}"})
    try:
        assert "orphan" in caption_for("c_prov_orphan")
    finally:
        db.execute("DELETE FROM clips WHERE id='c_prov_orphan'")
