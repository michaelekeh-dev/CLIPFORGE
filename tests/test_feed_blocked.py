"""A feed we cannot reach is not a channel that does not exist."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import db, autopilot  # noqa: E402

HANDLE = "https://www.youtube.com/@JumpersJump"
GIVEN = "UCzzzzzzzzzzzzzzzzzzzzzz"
LINK = f"https://www.youtube.com/channel/{GIVEN}"
SCRAPED = "UC-uFB6fwKkYkIi4658v_hxw"
VID = [{"id": "e1", "title": "EP.308", "url": "https://youtu.be/e1", "published": "2026-10-15"}]


@pytest.fixture()
def clean():
    db.init_db()
    db.execute("DELETE FROM settings WHERE key LIKE 'channel_id:%'")
    yield
    db.execute("DELETE FROM settings WHERE key LIKE 'channel_id:%'")


def feeds(monkeypatch, table):
    monkeypatch.setattr(autopilot, "_read_feed", lambda cid: table.get(cid, (404, [])))


def test_a_channel_that_really_does_not_exist_says_so(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [SCRAPED])
    feeds(monkeypatch, {})          # 404 for everything
    cid, _, why = autopilot.verified_channel_id(HANDLE)
    assert cid is None
    assert "says none of those channels exist" in why


def test_a_blocked_feed_is_not_reported_as_a_wrong_channel(clean, monkeypatch):
    """The message used to state as fact that YouTube knew none of them. It could not ask."""
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [SCRAPED])
    feeds(monkeypatch, {SCRAPED: (403, [])})
    cid, _, why = autopilot.verified_channel_id(HANDLE)
    assert cid is None
    assert "connection problem here, not a wrong channel" in why
    assert "none of those channels exist" not in why


def test_an_id_you_gave_yourself_survives_a_blocked_feed(clean, monkeypatch):
    """Refusing a correct id because the network is down is as wrong as believing a bad one."""
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [GIVEN])
    feeds(monkeypatch, {GIVEN: (429, [])})
    cid, vids, why = autopilot.verified_channel_id(LINK)
    assert cid == GIVEN and why == "", "you told it which channel; a timeout does not overrule that"
    assert vids == []


def test_an_id_you_gave_is_still_dropped_when_youtube_says_it_is_gone(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [GIVEN])
    feeds(monkeypatch, {GIVEN: (404, [])})
    cid, _, why = autopilot.verified_channel_id(LINK)
    assert cid is None and "none of those channels exist" in why


def test_a_scraped_id_is_never_taken_on_trust(clean, monkeypatch):
    """Only an id you supplied gets the benefit of the doubt, never one harvested off a page."""
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [SCRAPED])
    feeds(monkeypatch, {SCRAPED: (403, [])})
    cid, _, _ = autopilot.verified_channel_id(HANDLE)
    assert cid is None


def test_a_working_candidate_still_wins_over_everything(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [SCRAPED, GIVEN])
    feeds(monkeypatch, {SCRAPED: (403, []), GIVEN: (200, VID)})
    cid, vids, why = autopilot.verified_channel_id(LINK)
    assert cid == GIVEN and vids == VID and why == ""


def test_the_feed_is_fetched_through_the_proxy_when_one_is_set(monkeypatch):
    """YouTube refuses a datacenter IP, so the feed must go the same way the downloads do."""
    import httpx
    from clipforge import download
    monkeypatch.setattr(download, "proxy_url", lambda: "http://user:pw@proxy.test:823")
    seen = []

    class Resp:
        status_code, text = 200, '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    def fake_get(url, **kw):
        seen.append(kw.get("proxy"))
        return Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    autopilot._read_feed(GIVEN)
    assert seen and seen[0] == "http://user:pw@proxy.test:823"


def test_a_consent_page_is_treated_as_blocked_not_as_an_empty_channel(monkeypatch):
    """A 200 that is not XML is YouTube refusing, not a channel with no videos."""
    import httpx
    from clipforge import download
    monkeypatch.setattr(download, "proxy_url", lambda: "")

    class Resp:
        status_code, text = 200, "<html><body>Before you continue to YouTube</body></html>"

    monkeypatch.setattr(httpx, "get", lambda url, **kw: Resp())
    code, vids = autopilot._read_feed(GIVEN)
    assert code == autopilot.BLOCKED and vids == []


def test_a_typed_channel_id_is_used_exactly_as_given(clean, monkeypatch):
    """The way out of guessing: you paste the UC... id and it goes first, untouched."""
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "channel_id": GIVEN, "channel_url": HANDLE})
    assert autopilot.channel_candidates(HANDLE)[0] == GIVEN
    assert autopilot.explicit_channel_id(HANDLE) == GIVEN


def test_a_typed_id_survives_a_blocked_feed_too(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "channel_id": GIVEN, "channel_url": HANDLE})
    monkeypatch.setattr(autopilot, "channel_candidates", lambda u: [GIVEN])
    feeds(monkeypatch, {GIVEN: (autopilot.BLOCKED, [])})
    cid, _, why = autopilot.verified_channel_id(HANDLE)
    assert cid == GIVEN and why == ""


def test_rubbish_typed_in_is_ignored_rather_than_used(clean, monkeypatch):
    monkeypatch.setattr(autopilot, "get_settings",
                        lambda: {**autopilot.DEFAULTS, "channel_id": "not-an-id", "channel_url": HANDLE})
    assert autopilot.explicit_channel_id(HANDLE) == ""
    assert "not-an-id" not in autopilot.channel_candidates(HANDLE)
