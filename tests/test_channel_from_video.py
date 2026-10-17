"""Any video of a channel's identifies that channel — far easier than hunting for a UC id."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot, download, notify  # noqa: E402

CID = "UCzzzzzzzzzzzzzzzzzzzzzz"
LINK = "https://youtu.be/ODF8fPCd__w?si=xT-zOWnZa-dD6Ern"


@pytest.fixture()
def clean():
    db.init_db()
    before = db.get_setting("autopilot")
    db.execute("DELETE FROM settings WHERE key LIKE 'channel_id:%'")
    yield
    db.set_setting("autopilot", before or {})
    db.execute("DELETE FROM settings WHERE key LIKE 'channel_id:%'")


def test_every_shape_of_video_link_is_recognised():
    for u in ["https://youtu.be/ODF8fPCd__w?si=xT-zOWnZa-dD6Ern",
              "https://www.youtube.com/watch?v=ODF8fPCd__w",
              "https://www.youtube.com/watch?list=PL1&v=ODF8fPCd__w",
              "https://www.youtube.com/shorts/ODF8fPCd__w",
              "https://www.youtube.com/live/ODF8fPCd__w"]:
        assert autopilot.looks_like_video(u), u


def test_a_channel_link_is_not_mistaken_for_a_video():
    for u in ["https://www.youtube.com/@JumpersJump",
              f"https://www.youtube.com/channel/{CID}",
              "https://www.youtube.com/c/SomeName", ""]:
        assert not autopilot.looks_like_video(u), u


def test_the_channel_is_read_off_the_video(clean, monkeypatch):
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": CID, "channel": "Jumpers Jump",
                                                       "title": "EP.309", "duration": 5400})
    got = autopilot.channel_from_video(LINK)
    assert got["channel_id"] == CID and got["channel"] == "Jumpers Jump"


def test_a_video_whose_channel_cannot_be_read_gives_nothing(clean, monkeypatch):
    monkeypatch.setattr(download, "peek", lambda url: {})
    assert autopilot.channel_from_video(LINK) == {}
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": "rubbish"})
    assert autopilot.channel_from_video(LINK) == {}


def test_setting_the_channel_from_a_video_link_sticks(clean, monkeypatch):
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": CID, "channel": "Jumpers Jump"})
    out = autopilot.set_channel_from(LINK)
    assert out["ok"] and out["channel_id"] == CID
    assert "Jumpers Jump" in out["detail"]
    st = autopilot.get_settings()
    assert st["channel_id"] == CID
    assert CID in st["channel_url"], "the url is rewritten to the form that cannot be guessed wrong"


def test_setting_it_clears_any_stale_cached_id(clean, monkeypatch):
    db.set_setting("channel_id:https://www.youtube.com/@JumpersJump", "UC-uFB6fwKkYkIi4658v_hxw")
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": CID, "channel": "JJ"})
    autopilot.set_channel_from(LINK)
    assert db.get_setting("channel_id:https://www.youtube.com/@JumpersJump") is None


def test_a_channel_link_still_works(clean, monkeypatch):
    out = autopilot.set_channel_from(f"https://www.youtube.com/channel/{CID}")
    assert out["ok"] and autopilot.get_settings()["channel_id"] == CID


def test_an_unreadable_video_says_what_to_check(clean, monkeypatch):
    monkeypatch.setattr(download, "peek", lambda url: {})
    out = autopilot.set_channel_from(LINK)
    assert not out["ok"] and "proxy" in out["detail"]


def test_the_video_link_feeds_the_candidate_list(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings", lambda: {**autopilot.DEFAULTS, "channel_id": ""})
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": CID, "channel": "JJ"})
    assert autopilot.channel_candidates(LINK) == [CID]
    assert autopilot.explicit_channel_id(LINK) == CID, "a video link is as explicit as a /channel/ link"


def test_the_telegram_command_sets_it(clean, monkeypatch):
    monkeypatch.setattr(download, "peek", lambda url: {"channel_id": CID, "channel": "Jumpers Jump"})
    sent = []
    monkeypatch.setattr(notify, "send_text", lambda t, *a, **k: sent.append(t) or {"ok": True})
    notify.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": f"/channel {LINK}"}},
                         "https://app.test")
    assert sent and "Jumpers Jump" in sent[-1]
    assert autopilot.get_settings()["channel_id"] == CID
