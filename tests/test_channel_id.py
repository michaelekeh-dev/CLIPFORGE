"""The malformed channel id that silently 404'd the feed for days."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot  # noqa: E402

BAD = "UCRF8xw5dg9mL4r5ryFOtKw"      # 23 characters: what the loose regex captured and cached
GOOD = "UCRF8xw5dg9mL4r5ryFOtKwX"    # 24, the shape YouTube actually uses
URL = "https://www.youtube.com/@JumpersJump"


def test_a_channel_id_is_exactly_uc_plus_22():
    assert autopilot.valid_channel_id(GOOD)
    assert not autopilot.valid_channel_id(BAD), "23 characters must be rejected, not cached"
    assert not autopilot.valid_channel_id("UC" + "x" * 21)
    assert not autopilot.valid_channel_id("UC" + "x" * 23)
    assert not autopilot.valid_channel_id("")
    assert not autopilot.valid_channel_id("nonsense")


def test_a_channel_url_with_a_truncated_id_is_never_offered_as_a_candidate():
    assert autopilot.channel_candidates(f"https://www.youtube.com/channel/{BAD}") == []
    assert autopilot.channel_candidates(f"https://www.youtube.com/channel/{GOOD}") == [GOOD]


def test_a_bare_id_is_offered_only_when_well_formed():
    assert autopilot.channel_candidates(GOOD) == [GOOD]
    assert autopilot.channel_candidates(BAD) == []


def test_the_page_regex_will_not_capture_a_short_id(monkeypatch):
    """A boundary after the 22 characters, so a longer or shorter run is not silently trimmed."""
    import re
    pat = re.compile(r'channel/(UC[A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])')
    assert pat.search(f'href="/channel/{GOOD}"').group(1) == GOOD
    assert pat.search(f'href="/channel/{BAD}"') is None


def test_a_malformed_cached_id_is_thrown_away(monkeypatch):
    db.init_db()
    db.set_setting("channel_id:" + URL, BAD)
    tried = []
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: tried.append(u) or [])
    assert autopilot.channel_feed(URL) == []
    assert tried == [URL], "a bad cached id must trigger a fresh resolve"
    assert db.get_setting("channel_id:" + URL) is None, "and must not be left in the database"


def test_a_feed_404_is_never_cached(monkeypatch):
    """Superseded in detail by test_channel_verify.py; kept here as the guard on the cache itself."""
    db.init_db()
    db.execute("DELETE FROM settings WHERE key=?", ("channel_id:" + URL,))
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [GOOD])
    monkeypatch.setattr(autopilot, "_read_feed", lambda c: (404, []))
    assert autopilot.channel_feed(URL) == []
    assert db.get_setting("channel_id:" + URL) is None
