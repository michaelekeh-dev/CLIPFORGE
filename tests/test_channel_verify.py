"""A channel id is only believed once its feed has actually answered."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot  # noqa: E402

URL = "https://www.youtube.com/@JumpersJump"
WRONG = "UC-uFB6fwKkYkIi4658v_hxw"    # well formed, 24 chars, and YouTube 404s it
RIGHT = "UCzzzzzzzzzzzzzzzzzzzzzz"
VID = [{"id": "e1", "title": "EP.307", "url": "https://youtu.be/e1", "published": "2026-10-07"}]


@pytest.fixture()
def clean():
    db.init_db()
    db.execute("DELETE FROM settings WHERE key LIKE 'channel_id:%'")
    db.execute("DELETE FROM errors")
    yield
    db.execute("DELETE FROM settings WHERE key LIKE 'channel_id:%'")


def feeds(monkeypatch, table):
    """table: {channel_id: (status, videos)}"""
    seen = []

    def read(cid):
        seen.append(cid)
        return table.get(cid, (404, []))

    monkeypatch.setattr(autopilot, "_read_feed", read)
    return seen


def test_a_wrong_but_well_formed_id_is_not_believed(clean, monkeypatch):
    """The exact failure: a scraped id that looks perfect and 404s. It must not be cached."""
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [WRONG])
    feeds(monkeypatch, {RIGHT: (200, VID)})
    cid, vids, why = autopilot.verified_channel_id(URL)
    assert cid is None and vids == []
    assert WRONG in why and "address bar" in why
    assert autopilot.channel_feed(URL) == []
    assert db.get_setting("channel_id:" + URL) is None, "a 404ing id must never be cached"


def test_it_moves_on_to_the_next_candidate(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [WRONG, RIGHT])
    tried = feeds(monkeypatch, {RIGHT: (200, VID)})
    cid, vids, why = autopilot.verified_channel_id(URL)
    assert cid == RIGHT and vids == VID and why == ""
    assert tried == [WRONG, RIGHT], "the bad one is tried and discarded, not cached"


def test_only_a_working_id_is_cached(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [WRONG, RIGHT])
    feeds(monkeypatch, {RIGHT: (200, VID)})
    assert autopilot.channel_feed(URL) == VID
    assert db.get_setting("channel_id:" + URL) == RIGHT


def test_a_cached_id_that_stops_answering_is_replaced(clean, monkeypatch):
    db.set_setting("channel_id:" + URL, WRONG)
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [RIGHT])
    feeds(monkeypatch, {RIGHT: (200, VID)})
    assert autopilot.channel_feed(URL) == VID
    assert db.get_setting("channel_id:" + URL) == RIGHT


def test_an_empty_feed_counts_as_not_this_channel(clean, monkeypatch):
    """A 200 with no entries is someone else's empty channel, not ours."""
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [WRONG, RIGHT])
    feeds(monkeypatch, {WRONG: (200, []), RIGHT: (200, VID)})
    cid, vids, _ = autopilot.verified_channel_id(URL)
    assert cid == RIGHT and vids == VID


def test_no_candidates_at_all_says_what_to_paste(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [])
    cid, vids, why = autopilot.verified_channel_id(URL)
    assert cid is None and "/channel/UC" in why


def test_candidates_keep_page_order_and_drop_duplicates():
    out = autopilot.channel_candidates(f"https://www.youtube.com/channel/{RIGHT}")
    assert out == [RIGHT], "a direct /channel/ link needs no guessing"
    assert autopilot.channel_candidates("") == []
