"""The numbers page: it must read real uploads correctly and refuse to invent advice from thin data."""
import os
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import analytics  # noqa: E402

Z = timezone.utc
DAY = 86400


def vid(days_ago, views, hour=12, weekday=None, secs=45, n=0):
    """An upload N days ago, optionally forced onto a weekday/hour."""
    d = datetime.now(Z) - __import__("datetime").timedelta(days=days_ago)
    d = d.replace(hour=hour, minute=0, second=0, microsecond=0)
    if weekday is not None:
        d = d - __import__("datetime").timedelta(days=(d.weekday() - weekday) % 7)
    return {"id": f"v{n}", "title": f"clip {n}", "published": d.timestamp(), "seconds": secs,
            "views": views, "likes": 0, "comments": 0, "short": secs <= analytics.SHORT_MAX_SECONDS}


def test_duration_parsing():
    assert analytics.seconds("PT59S") == 59
    assert analytics.seconds("PT3M2S") == 182
    assert analytics.seconds("PT1H0M0S") == 3600
    assert analytics.seconds("garbage") == 0


def test_settled_drops_uploads_too_young_to_judge():
    vs = [vid(1, 9999, n=1), vid(30, 100, n=2)]
    assert [v["id"] for v in analytics.settled(vs)] == ["v2"]


def test_settled_drops_long_videos():
    vs = [vid(30, 100, secs=600, n=1), vid(30, 100, n=2)]
    assert [v["id"] for v in analytics.settled(vs)] == ["v2"]


def test_best_times_refuses_to_guess_from_a_handful():
    vs = [vid(30 + i, 100 + i, n=i) for i in range(5)]
    out = analytics.best_times(vs, Z)
    assert out["enough"] is False and out["need"] == analytics.MIN_VIDEOS
    assert out["days"] == [] and out["times"] == []


def test_best_times_ignores_an_hour_with_too_few_uploads():
    # 14 uploads at 09:00, one lucky upload at 03:00
    vs = [vid(30 + i, 100, hour=9, n=i) for i in range(14)]
    vs.append(vid(60, 100000, hour=3, n=99))
    out = analytics.best_times(vs, Z)
    assert out["enough"] is True
    assert "03:00" not in [h["label"] for h in out["hours"]]
    assert out["times"] == ["09:00"]


def test_best_times_finds_a_real_winner():
    vs = []
    for i in range(6):
        vs.append(vid(30 + i * 7, 1000, weekday=5, n=f"sat{i}"))   # Saturdays do well
    for i in range(6):
        vs.append(vid(33 + i * 7, 100, weekday=1, n=f"tue{i}"))    # Tuesdays do not
    out = analytics.best_times(vs, Z)
    assert out["days"][0]["label"] == "Saturday"
    assert out["days"][0]["times_normal"] > 1.5
    assert out["days"][-1]["label"] == "Tuesday"


def test_cadence_counts_the_last_month():
    vs = [vid(i, 100, n=i) for i in range(1, 15)]
    c = analytics.cadence(vs)
    assert c["last_30_days"] == 14
    assert c["per_week"] > 3
    assert c["median_gap_hours"] == pytest.approx(24, abs=1)


def test_advice_tells_a_quiet_channel_to_post():
    vs = [vid(200 + i, 100, n=i) for i in range(14)]
    sm = analytics.summary(vs, {})
    out = " ".join(analytics.advice(sm, analytics.best_times(vs, Z), analytics.cadence(vs)))
    assert "No Shorts in the last 30 days" in out


def test_advice_warns_about_weak_retention():
    vs = [vid(30 + i, 100, n=i) for i in range(14)]
    keep = {f"v{i}": {"kept_percent": 40.0, "subs_gained": 1} for i in range(14)}
    sm = analytics.summary(vs, keep)
    out = " ".join(analytics.advice(sm, analytics.best_times(vs, Z), analytics.cadence(vs)))
    assert "losing them" in out


def test_advice_offers_the_extra_tap_when_retention_is_not_allowed(monkeypatch):
    monkeypatch.setattr(analytics, "has_analytics", lambda: False)
    vs = [vid(30 + i, 100, n=i) for i in range(14)]
    sm = analytics.summary(vs, {})
    out = " ".join(analytics.advice(sm, analytics.best_times(vs, Z), analytics.cadence(vs)))
    assert "one extra tap" in out


def test_summary_picks_the_best_clip():
    vs = [vid(30, 50, n=1), vid(31, 5000, n=2), vid(32, 90, n=3)]
    vs[1]["title"] = "the good one"
    sm = analytics.summary(vs, {})
    assert sm["best_title"] == "the good one" and sm["best_views"] == 5000


def test_retention_is_empty_without_the_scope(monkeypatch):
    monkeypatch.setattr(analytics, "has_analytics", lambda: False)
    assert analytics.retention(["abc"]) == {}


def test_report_says_so_when_youtube_is_not_connected(monkeypatch):
    from clipforge import youtube
    monkeypatch.setattr(youtube, "connected", lambda: False)
    monkeypatch.setattr(analytics.db, "get_setting", lambda k: None)
    out = analytics.report(force=True)
    assert out["ok"] is False and "Connect" in out["detail"]


def test_a_below_normal_hour_is_never_offered_as_a_slot():
    vs = [vid(30 + i * 7, 1000, hour=18, n=f"good{i}") for i in range(6)]
    vs += [vid(33 + i * 7, 100, hour=4, n=f"bad{i}") for i in range(6)]
    out = analytics.best_times(vs, Z)
    # both hours have enough uploads to be listed...
    assert {h["label"] for h in out["hours"]} == {"18:00", "04:00"}
    # ...but only the one that beats a normal upload is offered as a posting time
    assert out["times"] == ["18:00"]
