"""YouTube: connect the channel once (OAuth), then upload clips with title, description, hashtags and a publish time."""
from __future__ import annotations
import json
import time
from pathlib import Path
from . import db
from .config import env

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly",
          # retention and traffic sources, so the Numbers page can say what is actually working
          "https://www.googleapis.com/auth/yt-analytics.readonly"]


def configured() -> bool:
    return bool(env("YOUTUBE_CLIENT_ID") and env("YOUTUBE_CLIENT_SECRET"))


def _client_config(redirect_uri: str) -> dict:
    return {"web": {"client_id": env("YOUTUBE_CLIENT_ID"), "client_secret": env("YOUTUBE_CLIENT_SECRET"),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]}}


def auth_url(redirect_uri: str) -> str:
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    db.set_setting("youtube_oauth_state", state)
    # the login finishes in a second request, so the PKCE verifier has to survive in between
    db.set_setting("youtube_oauth_verifier", getattr(flow, "code_verifier", None))
    return url


def finish_auth(redirect_uri: str, code: str) -> dict:
    from google_auth_oauthlib.flow import Flow
    verifier = db.get_setting("youtube_oauth_verifier")
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES, redirect_uri=redirect_uri,
                                   code_verifier=verifier, autogenerate_code_verifier=not verifier)
    flow.fetch_token(code=code)
    db.execute("DELETE FROM settings WHERE key IN ('youtube_oauth_verifier','youtube_oauth_state')")
    c = flow.credentials
    db.set_setting("youtube_credentials", {"token": c.token, "refresh_token": c.refresh_token, "token_uri": c.token_uri,
                                           "client_id": c.client_id, "client_secret": c.client_secret, "scopes": list(c.scopes or SCOPES)})
    info = channel_info(force=True)
    return info


def credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    data = db.get_setting("youtube_credentials")
    if not data or not data.get("refresh_token"):
        return None
    creds = Credentials(**{k: data.get(k) for k in ("token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes")})
    if not creds.valid:
        creds.refresh(Request())
        data["token"] = creds.token
        db.set_setting("youtube_credentials", data)
    return creds


def connected() -> bool:
    return bool((db.get_setting("youtube_credentials") or {}).get("refresh_token"))


def disconnect():
    db.execute("DELETE FROM settings WHERE key IN ('youtube_credentials','youtube_channel')")


def channel_info(force: bool = False) -> dict:
    cached = db.get_setting("youtube_channel")
    if cached and not force:
        return cached
    creds = credentials()
    if not creds:
        return {}
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    r = yt.channels().list(part="snippet,statistics", mine=True).execute()
    items = r.get("items") or []
    if not items:
        return {}
    ch = items[0]
    info = {"id": ch["id"], "title": ch["snippet"]["title"], "handle": ch["snippet"].get("customUrl", ""),
            "subscribers": (ch.get("statistics") or {}).get("subscriberCount", "")}
    db.set_setting("youtube_channel", info)
    return info


def upload(path: str, title: str, description: str, tags: list[str], publish_at: float | None = None,
           public: bool = True, category_id: str = "24", made_for_kids: bool = False) -> dict:
    """Upload a clip. Returns {'id': videoId, 'status': 'scheduled'|'public'|'private'}."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = credentials()
    if not creds:
        raise RuntimeError("YouTube is not connected")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    status = {"selfDeclaredMadeForKids": made_for_kids}
    if public and publish_at and publish_at > time.time() + 60:
        from datetime import datetime, timezone
        status.update({"privacyStatus": "private", "publishAt": datetime.fromtimestamp(publish_at, timezone.utc).isoformat().replace("+00:00", "Z")})
        kind = "scheduled"
    elif public:
        status["privacyStatus"] = "public"
        kind = "public"
    else:
        status["privacyStatus"] = "private"
        kind = "private"
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": [t.lstrip("#")[:30] for t in tags][:30],
                        "categoryId": category_id}, "status": status}
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    tries = 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except Exception as e:  # noqa: BLE001
            tries += 1
            if tries > 5:
                raise
            time.sleep(2 * tries)
    return {"id": resp["id"], "status": kind}
