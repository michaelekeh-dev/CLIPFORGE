"""The Telegram listener is tested against a fake Telegram server (no network needed)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class FakeTelegram(BaseHTTPRequestHandler):
    updates: list = []
    sent: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_raw": raw[:200].decode("utf-8", "ignore")}
        method = self.path.rsplit("/", 1)[-1]
        if method == "getMe":
            out = {"ok": True, "result": {"username": "truthuntold_clips_bot"}}
        elif method == "getUpdates":
            out = {"ok": True, "result": FakeTelegram.updates}
            FakeTelegram.updates = []
        else:
            FakeTelegram.sent.append({"method": method, "body": body})
            out = {"ok": True, "result": {"message_id": 1, "chat": {"id": 42}}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def _server():
    srv = HTTPServer(("127.0.0.1", 0), FakeTelegram)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture()
def fake_telegram(_server, monkeypatch):
    """One fake Telegram for the whole module: the listener thread is long-lived, like in the real app."""
    FakeTelegram.updates, FakeTelegram.sent = [], []
    from clipforge import notify
    notify.state["backoff"] = 0  # a listener that was failing against the real API should not stall the test
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_API_BASE", f"http://127.0.0.1:{_server.server_port}")
    from clipforge import db
    db.init_db()
    yield _server


def _wait(check, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


def test_start_links_the_chat_and_commands_answer(fake_telegram, monkeypatch):
    from clipforge import notify, db
    db.execute("DELETE FROM settings WHERE key='telegram_chat_id'")
    notify.start_poller(lambda: "https://app.test")
    assert _wait(lambda: notify.state["running"]), "listener did not start"

    FakeTelegram.updates = [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/start"}}]
    # wait for the reply, not the setting: the setting is written first, so waiting on it races the reply
    assert _wait(lambda: any(s["method"] == "sendMessage" and "Linked" in s["body"]["text"]
                             for s in FakeTelegram.sent)), "no Linked reply"
    assert db.get_setting("telegram_chat_id") == "42", "chat was not linked"
    assert notify.state["bot"] == "truthuntold_clips_bot"

    FakeTelegram.sent.clear()
    FakeTelegram.updates = [{"update_id": 2, "message": {"chat": {"id": 42}, "text": "/status"}}]
    assert _wait(lambda: any(s["method"] == "sendMessage" for s in FakeTelegram.sent)), "no answer to /status"


def test_a_link_starts_a_project(fake_telegram, monkeypatch):
    from clipforge import notify, db, autopilot
    started = []
    monkeypatch.setattr(autopilot, "start_project_from_url", lambda url, title="": started.append(url) or "p_test")
    db.set_setting("telegram_chat_id", "42")
    notify.start_poller(lambda: "https://app.test")
    assert _wait(lambda: notify.state["running"])
    FakeTelegram.updates = [{"update_id": 3, "message": {"chat": {"id": 42}, "text": "https://www.youtube.com/watch?v=abc123"}}]
    assert _wait(lambda: started), "the link did not start a project"
    assert started[0].endswith("v=abc123")


def test_buttons_queue_and_skip(fake_telegram, monkeypatch):
    from clipforge import notify, db, autopilot
    calls = []
    monkeypatch.setattr(autopilot, "queue_post", lambda cid: calls.append(("post", cid)) or {"publish_at": time.time() + 60})
    monkeypatch.setattr(autopilot, "skip_clip", lambda cid: calls.append(("skip", cid)))
    monkeypatch.setattr(notify, "refresh_clip_message", lambda *a, **k: None)
    db.set_setting("telegram_chat_id", "42")
    notify.start_poller(lambda: "https://app.test")
    assert _wait(lambda: notify.state["running"])
    FakeTelegram.updates = [
        {"update_id": 4, "callback_query": {"id": "q1", "data": "post:c_1"}},
        {"update_id": 5, "callback_query": {"id": "q2", "data": "skip:c_2"}},
    ]
    assert _wait(lambda: len(calls) == 2), f"buttons did not work: {calls}"
    assert calls == [("post", "c_1"), ("skip", "c_2")]
    assert any(s["method"] == "answerCallbackQuery" for s in FakeTelegram.sent)


def _clip(db, clip_id="c_edit1"):
    """A finished clip with a project, ready to post."""
    db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    db.execute("DELETE FROM posts WHERE clip_id=?", (clip_id,))
    if not db.row("SELECT id FROM projects WHERE id='p_edit'"):
        db.insert("projects", {"id": "p_edit", "title": "ep", "status": "done", "source_url": "https://youtu.be/x",
                               "options": json.dumps({"credit_name": "Jumpers Jump"}), "info": "{}"})
    db.insert("clips", {"id": clip_id, "project_id": "p_edit", "status": "done", "start": 0, "end": 45, "score": 80,
                        "title": "The orgone cloud buster", "path": __file__,
                        "data": json.dumps({"description": "a body line", "hashtags": ["#Shorts"]}), "settings": "{}"})
    return db.loads(db.row("SELECT * FROM clips WHERE id=?", (clip_id,)), "data", "settings")


def test_the_caption_shows_the_real_youtube_title_and_description(fake_telegram):
    from clipforge import notify, db
    clip = _clip(db)
    cap = notify.clip_caption(clip, None, "https://app.test")
    # the title YouTube gets, not just the clip name
    assert "The orgone cloud buster #Shorts" in cap
    assert "📝" in cap and "a body line" in cap


def test_the_buttons_offer_post_now_schedule_and_editing():
    from clipforge import notify
    rows = notify.clip_buttons("c_1", None)
    flat = [b["callback_data"] for r in rows for b in r]
    assert "now:c_1" in flat and "when:c_1" in flat
    assert "edit:c_1:title" in flat and "edit:c_1:desc" in flat


def test_a_queued_clip_can_still_be_posted_now_or_moved():
    from clipforge import notify
    flat = [b["callback_data"] for r in notify.clip_buttons("c_1", {"status": "waiting"}) for b in r]
    assert "now:c_1" in flat and "when:c_1" in flat and "cancel:c_1" in flat


def test_an_uploaded_clip_has_no_buttons_left():
    from clipforge import notify
    assert notify.clip_buttons("c_1", {"status": "uploaded"}) == []
    assert notify.clip_buttons("c_1", {"status": "uploading"}) == []


def test_post_now_uploads_instead_of_queueing(fake_telegram, monkeypatch):
    from clipforge import notify, db, autopilot
    clip = _clip(db, "c_now1")
    done = {}
    monkeypatch.setattr(autopilot, "upload_post", lambda p: done.update(p) or True)
    p = autopilot.post_now("c_now1")
    assert p and abs(p["publish_at"] - time.time()) < 5, "Post now must publish now, not at the next slot"
    assert _wait(lambda: done.get("clip_id") == "c_now1"), "the upload never started"


def test_choosing_a_time_moves_the_post(fake_telegram):
    from clipforge import db, autopilot
    _clip(db, "c_when1")
    when = time.time() + 7200
    p = autopilot.reschedule("c_when1", when)
    assert p and abs(p["publish_at"] - when) < 2 and p["status"] == "waiting"


def test_typing_a_new_title_changes_what_youtube_gets(fake_telegram):
    from clipforge import notify, db
    _clip(db, "c_t1")
    notify.handle_update({"update_id": 90, "callback_query": {
        "id": "q1", "data": "edit:c_t1:title",
        "message": {"message_id": 5, "chat": {"id": 42}}}}, "https://app.test")
    assert db.get_setting("tg_edit:42")["clip_id"] == "c_t1"
    notify.handle_update({"update_id": 91, "message": {"chat": {"id": 42}, "text": "Uncovered: the cloud buster"}},
                         "https://app.test")
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", ("c_t1",)), "data", "settings")
    title, _ = notify.upload_text(clip)
    assert title.startswith("Uncovered: the cloud buster")
    assert db.get_setting("tg_edit:42") is None, "the edit prompt should be finished with"


def test_typing_a_new_description_keeps_the_credit_footer(fake_telegram):
    from clipforge import notify, db
    _clip(db, "c_d1")
    notify.handle_update({"update_id": 92, "callback_query": {
        "id": "q2", "data": "edit:c_d1:desc",
        "message": {"message_id": 6, "chat": {"id": 42}}}}, "https://app.test")
    notify.handle_update({"update_id": 93, "message": {"chat": {"id": 42}, "text": "my own words"}}, "https://app.test")
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", ("c_d1",)), "data", "settings")
    _, desc = notify.upload_text(clip)
    assert desc.startswith("my own words")
    assert "Jumpers Jump" in desc, "the credit footer must survive an edit"


def test_cancel_leaves_the_text_alone(fake_telegram):
    from clipforge import notify, db
    _clip(db, "c_c1")
    notify.handle_update({"update_id": 94, "callback_query": {
        "id": "q3", "data": "edit:c_c1:title",
        "message": {"message_id": 7, "chat": {"id": 42}}}}, "https://app.test")
    notify.handle_update({"update_id": 95, "message": {"chat": {"id": 42}, "text": "/cancel"}}, "https://app.test")
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", ("c_c1",)), "data", "settings")
    assert not (clip.get("data") or {}).get("post_title")
    assert db.get_setting("tg_edit:42") is None


def test_a_command_is_never_swallowed_by_a_pending_edit(fake_telegram):
    from clipforge import notify, db
    _clip(db, "c_s1")
    db.set_setting("tg_edit:42", {"clip_id": "c_s1", "field": "title"})
    FakeTelegram.sent.clear()
    notify.handle_update({"update_id": 96, "message": {"chat": {"id": 42}, "text": "/status"}}, "https://app.test")
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", ("c_s1",)), "data", "settings")
    assert not (clip.get("data") or {}).get("post_title"), "/status must not become the title"
    db.execute("DELETE FROM settings WHERE key='tg_edit:42'")


def test_schedule_buttons_offer_real_times(fake_telegram):
    from clipforge import notify
    rows = notify.time_buttons("c_1")
    flat = [b for r in rows for b in r]
    assert any(b["callback_data"].startswith("at:c_1:") for b in flat)
    assert flat[0]["text"] == "In 1 hour"
    assert any(b["callback_data"] == "back:c_1" for b in flat)


def test_post_now_refuses_to_upload_the_same_clip_twice(fake_telegram, monkeypatch):
    from clipforge import db, autopilot
    _clip(db, "c_twice")
    calls = []
    monkeypatch.setattr(autopilot, "upload_post", lambda p: calls.append(p["id"]) or True)
    assert autopilot.post_now("c_twice") is not None
    assert _wait(lambda: len(calls) == 1)
    db.execute("UPDATE posts SET status='uploaded' WHERE clip_id=?", ("c_twice",))
    assert autopilot.post_now("c_twice") is None, "a clip already on YouTube must not be posted again"
    time.sleep(0.3)
    assert len(calls) == 1


def test_schedule_offers_several_different_times(fake_telegram):
    from clipforge import notify, autopilot
    slots = autopilot.slot_choices(3)
    assert len(slots) == 3 and slots == sorted(slots) and len(set(slots)) == 3, \
        "each Schedule button must be a different, later time"
    labels = [b["text"] for r in notify.time_buttons("c_1") for b in r]
    assert len(set(labels)) == len(labels), "no duplicate times on the keyboard"
