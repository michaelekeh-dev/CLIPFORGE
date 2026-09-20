"""The YouTube download retry chain, tested with a fake yt-dlp (no network needed)."""
import pytest
import yt_dlp

from clipforge import download as d


class FakeYDL:
    """Records every attempt and fails according to `script`, keyed by player client name."""
    attempts: list = []
    script: dict = {}
    made_file = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def _client(self):
        ex = self.opts.get("extractor_args") or {}
        yt = ex.get("youtube") or {}
        c = (yt.get("player_client") or ["default"])[0]
        if not self.opts.get("cookiefile"):
            c += " without cookies"
        return c + (" (any format)" if self.opts.get("format") == "best" else "")

    def extract_info(self, url, download=False):
        return {"id": "vid123", "title": "Episode", "channel": "Jumpers Jump", "duration": 3600,
                "thumbnail": "", "webpage_url": url, "upload_date": "20260101"}

    def download(self, urls):
        FakeYDL.attempts.append(self._client)
        err = FakeYDL.script.get(self._client)
        if err:
            raise Exception(err)
        if FakeYDL.made_file:
            FakeYDL.made_file()

    def sanitize_info(self, info):
        return info

    def download_with_info_file(self, path):
        FakeYDL.attempts.append(self._client + (" via proxy" if self.opts.get("proxy") else " direct"))
        err = FakeYDL.script.get(self._client + (" via proxy" if self.opts.get("proxy") else " direct"))
        if err:
            raise Exception(err)
        if FakeYDL.made_file:
            FakeYDL.made_file()


@pytest.fixture()
def fake_ytdlp(monkeypatch, tmp_path):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(d, "DL_DIR", tmp_path)
    monkeypatch.setattr(d.time, "sleep", lambda *_: None)
    FakeYDL.attempts, FakeYDL.script = [], {}
    FakeYDL.made_file = lambda: (tmp_path / "vid123.mp4").write_bytes(b"video")
    return FakeYDL


BOT = "ERROR: [youtube] vid123: Sign in to confirm you're not a bot."
FMT = "ERROR: [youtube] vid123: Requested format is not available."
DEAD_COOKIES = ("ERROR: The provided YouTube account cookies are no longer valid. "
                "They have likely been rotated in the browser as a security measure.")


@pytest.fixture()
def with_cookies(monkeypatch, tmp_path):
    ck = tmp_path / "cookies.txt"
    ck.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(d, "_cookie_file", lambda: str(ck))
    return str(ck)


def test_rotated_cookies_are_dropped_and_the_download_still_works(fake_ytdlp, with_cookies):
    FakeYDL.script = {"default": DEAD_COOKIES}
    meta = d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default", "default without cookies"] and meta["path"]


def test_once_cookies_prove_dead_later_clients_skip_them(fake_ytdlp, with_cookies):
    FakeYDL.script = {"default": DEAD_COOKIES, "default without cookies": BOT, "tv without cookies": BOT}
    d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default", "default without cookies", "tv without cookies", "mweb without cookies"]
    assert not any(a in ("tv", "mweb") for a in FakeYDL.attempts)


def test_good_cookies_are_used_first(fake_ytdlp, with_cookies):
    meta = d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default"] and meta["path"]


def test_the_message_names_a_missing_js_solver(monkeypatch):
    monkeypatch.setattr(d, "js_solver_status", lambda: {"runtime": "", "ejs": "", "ok": False, "remote": []})
    msg = str(d._classify(Exception(FMT)))
    assert "JavaScript challenge" in msg and "deno" in msg


def test_first_client_wins_without_extra_attempts(fake_ytdlp):
    meta = d.download("https://youtu.be/vid123")
    assert meta["title"] == "Episode" and meta["path"].endswith("vid123.mp4")
    assert FakeYDL.attempts == ["default without cookies"]  # no cookies configured in this test


def test_falls_through_clients_until_one_works(fake_ytdlp):
    FakeYDL.script = {"default without cookies": BOT, "tv without cookies": BOT}
    meta = d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default without cookies", "tv without cookies", "mweb without cookies"]
    assert meta["path"]


def test_a_format_problem_retries_the_same_client_with_any_format(fake_ytdlp):
    FakeYDL.script = {"default without cookies": FMT}
    meta = d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default without cookies", "default without cookies (any format)"] and meta["path"]


def test_a_refusal_does_not_retry_the_same_client(fake_ytdlp):
    FakeYDL.script = {"default without cookies": BOT}
    d.download("https://youtu.be/vid123")
    assert not any("any format" in a for a in FakeYDL.attempts)


def test_when_everything_fails_the_refusal_is_reported_not_the_format(fake_ytdlp):
    FakeYDL.script = {c + " without cookies" + suffix: (FMT if c == "android_vr" else BOT)
                      for c in ["default", "tv", "mweb", "web_safari", "android_vr"] for suffix in ("", " (any format)")}
    FakeYDL.made_file = None
    with pytest.raises(d.DownloadBlocked) as err:
        d.download("https://youtu.be/vid123")
    assert "blocked the download" in str(err.value)
    assert len([a for a in FakeYDL.attempts if "any format" not in a]) == 5


def test_a_pure_format_problem_says_so_plainly(fake_ytdlp):
    FakeYDL.script = {c + " without cookies" + suffix: FMT
                      for c in ["default", "tv", "mweb", "web_safari", "android_vr"] for suffix in ("", " (any format)")}
    FakeYDL.made_file = None
    with pytest.raises(d.DownloadBlocked) as err:
        d.download("https://youtu.be/vid123")
    assert "held back every video stream" in str(err.value) and "blocked the download" not in str(err.value)


def test_a_proxy_is_used_for_the_page_and_not_for_the_video(fake_ytdlp, monkeypatch):
    """The expensive part must not go through a paid proxy unless it has to."""
    monkeypatch.setenv("YTDLP_PROXY", "http://user:pass@proxy:8080")
    meta = d.download("https://youtu.be/vid123")
    assert meta["path"]
    assert "default without cookies direct" in FakeYDL.attempts
    assert "default without cookies via proxy" not in FakeYDL.attempts


def test_address_bound_links_fall_back_to_the_proxy(fake_ytdlp, monkeypatch):
    monkeypatch.setenv("YTDLP_PROXY", "http://user:pass@proxy:8080")
    FakeYDL.script = {"default without cookies direct": "ERROR: HTTP Error 403: Forbidden"}
    meta = d.download("https://youtu.be/vid123")
    assert meta["path"]
    assert FakeYDL.attempts[-1] == "default without cookies via proxy"
