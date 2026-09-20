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


def test_first_client_wins_without_extra_attempts(fake_ytdlp):
    meta = d.download("https://youtu.be/vid123")
    assert meta["title"] == "Episode" and meta["path"].endswith("vid123.mp4")
    assert FakeYDL.attempts == ["default"]


def test_falls_through_clients_until_one_works(fake_ytdlp):
    FakeYDL.script = {"default": BOT, "tv": BOT}
    meta = d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default", "tv", "mweb"] and meta["path"]


def test_a_format_problem_retries_the_same_client_with_any_format(fake_ytdlp):
    FakeYDL.script = {"default": FMT}
    meta = d.download("https://youtu.be/vid123")
    assert FakeYDL.attempts == ["default", "default (any format)"] and meta["path"]


def test_a_refusal_does_not_retry_the_same_client(fake_ytdlp):
    FakeYDL.script = {"default": BOT}
    d.download("https://youtu.be/vid123")
    assert "default (any format)" not in FakeYDL.attempts


def test_when_everything_fails_the_refusal_is_reported_not_the_format(fake_ytdlp):
    FakeYDL.script = {c: (FMT if c.startswith("android_vr") else BOT) for c in
                      ["default", "tv", "mweb", "web_safari", "android_vr", "android_vr (any format)"]}
    FakeYDL.made_file = None
    with pytest.raises(d.DownloadBlocked) as err:
        d.download("https://youtu.be/vid123")
    assert "blocked the download" in str(err.value)
    assert len([a for a in FakeYDL.attempts if "any format" not in a]) == 5


def test_a_pure_format_problem_says_so_plainly(fake_ytdlp):
    FakeYDL.script = {c: FMT for c in ["default", "tv", "mweb", "web_safari", "android_vr",
                                       "default (any format)", "tv (any format)", "mweb (any format)",
                                       "web_safari (any format)", "android_vr (any format)"]}
    FakeYDL.made_file = None
    with pytest.raises(d.DownloadBlocked) as err:
        d.download("https://youtu.be/vid123")
    assert "no video we could use" in str(err.value) and "blocked" not in str(err.value)
