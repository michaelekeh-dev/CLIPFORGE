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


def test_a_channel_url_with_a_truncated_id_is_refused():
    assert autopilot.resolve_channel_id(f"https://www.youtube.com/channel/{BAD}") is None
    assert autopilot.resolve_channel_id(f"https://www.youtube.com/channel/{GOOD}") == GOOD


def test_a_bare_id_is_accepted_only_when_well_formed():
    assert autopilot.resolve_channel_id(GOOD) == GOOD
    assert autopilot.resolve_channel_id(BAD) is None


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
    monkeypatch.setattr(autopilot, "resolve_channel_id", lambda u: tried.append(u) or None)
    assert autopilot.channel_feed(URL) == []
    assert tried == [URL], "a bad cached id must trigger a fresh resolve"
    assert db.get_setting("channel_id:" + URL) is None, "and must not be left in the database"


def test_a_feed_404_forgets_the_id_and_resolves_again(monkeypatch):
    db.init_db()
    db.set_setting("channel_id:" + URL, GOOD)

    class Resp:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"Client error '{self.status_code}'")

    FEED = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            '<entry><yt:videoId>abc</yt:videoId><title>EP.300</title><published>2026-09-01</published></entry></feed>')
    calls = []
    import httpx
    other = "UCzzzzzzzzzzzzzzzzzzzzzz"

    def fake_get(url, **kw):
        calls.append(url)
        return Resp(404) if GOOD in url else Resp(200, FEED)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(autopilot, "resolve_channel_id", lambda u: other)
    out = autopilot.channel_feed(URL)
    assert len(calls) == 2, "it must retry once with a freshly resolved id"
    assert out and out[0]["id"] == "abc"
    assert db.get_setting("channel_id:" + URL) == other


def test_a_404_that_resolves_to_the_same_id_gives_up_quietly(monkeypatch):
    db.init_db()
    db.set_setting("channel_id:" + URL, GOOD)

    class Resp:
        status_code, text = 404, ""

        def raise_for_status(self):
            raise RuntimeError("404")

    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, **kw: Resp())
    monkeypatch.setattr(autopilot, "resolve_channel_id", lambda u: GOOD)
    assert autopilot.channel_feed(URL) == [], "no infinite retry loop"
