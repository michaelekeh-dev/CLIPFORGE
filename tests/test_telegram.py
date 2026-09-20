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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_API_BASE", f"http://127.0.0.1:{_server.server_port}")
    from clipforge import db
    db.init_db()
    yield _server


def _wait(check, seconds=10):
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
    assert _wait(lambda: db.get_setting("telegram_chat_id") == "42"), "chat was not linked"
    assert any(s["method"] == "sendMessage" and "Linked" in s["body"]["text"] for s in FakeTelegram.sent)
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
