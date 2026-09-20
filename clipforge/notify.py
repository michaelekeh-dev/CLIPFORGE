"""Telegram: every finished clip lands in your chat with its fact-check verdict and Post / Skip buttons."""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
import httpx
from . import db
from .config import env

API_BASE = env("TELEGRAM_API_BASE") or "https://api.telegram.org"
API = "{base}/bot{token}/{method}"

# what the listener is doing, shown on the Autopilot page
state: dict = {"running": False, "last_ok": 0.0, "last_error": "", "updates": 0, "bot": "", "backoff": 0}


def enabled() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN"))


def chat_id() -> str | None:
    return env("TELEGRAM_CHAT_ID") or db.get_setting("telegram_chat_id")


def _call(method: str, timeout: float = 30, **data):
    files = data.pop("_files", None)
    if "timeout_" in data:  # long polling: Telegram's own timeout parameter
        data.pop("timeout_")
        data["timeout"] = 50
    url = API.format(base=env("TELEGRAM_API_BASE") or API_BASE, token=env("TELEGRAM_BOT_TOKEN"), method=method)
    try:
        if files:
            r = httpx.post(url, data=data, files=files, timeout=timeout)
        else:
            r = httpx.post(url, json=data, timeout=timeout)
        out = r.json()
        if out.get("ok"):
            state["last_ok"] = time.time()
        else:
            state["last_error"] = f"{method}: {str(out)[:200]}"
            db.log_error("telegram", f"{method}: {out}")
        return out
    except Exception as e:  # noqa: BLE001
        state["last_error"] = f"{method}: {str(e)[:200]}"
        db.log_error("telegram", f"{method}: {e}")
        return {"ok": False, "error": str(e)}


def whoami() -> dict:
    """The bot's own name, and a quick check that the token works."""
    if not enabled():
        return {}
    out = _call("getMe", timeout=15)
    if out.get("ok"):
        state["bot"] = out["result"].get("username", "")
        return out["result"]
    return {}


def send_text(text: str, chat: str | None = None, buttons: list[list[dict]] | None = None) -> dict:
    chat = chat or chat_id()
    if not enabled() or not chat:
        return {"ok": False}
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        data["reply_markup"] = {"inline_keyboard": buttons}
    return _call("sendMessage", **data)


def clip_caption(clip: dict, post: dict | None, base_url: str) -> str:
    from .factcheck import badge
    d = clip.get("data") or {}
    fc = d.get("fact_check") or {}
    dur = round((clip["end"] or 0) - (clip["start"] or 0))
    lines = [f"<b>{_esc(clip['title'])}</b>", f"{badge(fc.get('verdict', ''))} {_esc(fc.get('verdict', 'not checked'))} · {_esc(fc.get('type', ''))} · {dur}s · score {int(clip['score'] or 0)}"]
    if fc.get("summary"):
        lines.append(_esc(fc["summary"])[:300])
    if fc.get("red_flags"):
        lines.append("⚠️ " + _esc("; ".join(fc["red_flags"])[:300]))
    if post:
        if post["status"] == "waiting":
            lines.append(f"🕒 Posts at {_fmt_time(post['publish_at'])} unless you cancel.")
        elif post["status"] in ("uploaded", "scheduled", "published"):
            lines.append(f"✅ On YouTube: https://youtu.be/{post['youtube_id']}" if post.get("youtube_id") else "✅ Uploaded")
        elif post["status"] == "skipped":
            lines.append("⏭ Skipped")
        elif post["status"] == "error":
            lines.append("❌ " + _esc(post.get("error", "")[:200]))
    lines.append(f"✏️ {base_url}/clip/{clip['id']}")
    return "\n".join(lines)


def clip_buttons(clip_id: str, post: dict | None) -> list[list[dict]]:
    st = (post or {}).get("status", "")
    if st == "waiting":
        return [[{"text": "🚫 Cancel post", "callback_data": f"cancel:{clip_id}"}]]
    if st in ("uploaded", "scheduled", "published", "uploading"):
        return []
    return [[{"text": "✅ Post", "callback_data": f"post:{clip_id}"}, {"text": "⏭ Skip", "callback_data": f"skip:{clip_id}"}]]


def send_clip(clip_id: str, base_url: str) -> dict:
    """Send the rendered clip video with its verdict and buttons."""
    chat = chat_id()
    if not enabled() or not chat:
        return {"ok": False}
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", (clip_id,)), "data", "settings")
    if not clip or not clip.get("path") or not Path(clip["path"]).exists():
        return {"ok": False}
    post = db.row("SELECT * FROM posts WHERE clip_id=? ORDER BY created_at DESC LIMIT 1", (clip_id,))
    caption = clip_caption(clip, post, base_url)
    buttons = clip_buttons(clip_id, post)
    data = {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML", "supports_streaming": "true",
            "reply_markup": json.dumps({"inline_keyboard": buttons})}
    size = Path(clip["path"]).stat().st_size
    if size < 49 * 1024 * 1024:
        with open(clip["path"], "rb") as f:
            out = _call("sendVideo", timeout=180, _files={"video": (Path(clip["path"]).name, f, "video/mp4")}, **data)
    else:
        out = _call("sendMessage", chat_id=chat, text=caption + "\n(clip too big for Telegram, open the link)", parse_mode="HTML",
                    reply_markup={"inline_keyboard": buttons})
    if out.get("ok"):
        msg = out["result"]
        if post:
            db.update("posts", post["id"], {"telegram_msg": f"{msg['chat']['id']}:{msg['message_id']}"})
        db.execute("UPDATE clips SET data = json_set(data, '$.telegram_msg', ?) WHERE id=?", (f"{msg['chat']['id']}:{msg['message_id']}", clip_id))
    return out


def refresh_clip_message(clip_id: str, base_url: str):
    """Update caption + buttons of the clip's Telegram message after a decision."""
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", (clip_id,)), "data", "settings")
    if not clip:
        return
    ref = (clip.get("data") or {}).get("telegram_msg")
    if not ref:
        return
    chat, mid = ref.split(":")
    post = db.row("SELECT * FROM posts WHERE clip_id=? ORDER BY created_at DESC LIMIT 1", (clip_id,))
    _call("editMessageCaption", chat_id=chat, message_id=int(mid), caption=clip_caption(clip, post, base_url)[:1024],
          parse_mode="HTML", reply_markup={"inline_keyboard": clip_buttons(clip_id, post)})


# ----------------------------------------------------------------------------- incoming
def handle_update(u: dict, base_url: str):
    from . import autopilot
    if "message" in u:
        m = u["message"]
        text = (m.get("text") or "").strip()
        chat = str(m["chat"]["id"])
        if text.startswith("/start"):
            db.set_setting("telegram_chat_id", chat)
            send_text("Linked. Finished clips will show up here with Post / Skip buttons.\nCommands: /status, /post <link> (start a new episode), /pause, /resume", chat)
        elif text.startswith("/status"):
            send_text(autopilot.status_text(), chat)
        elif text.startswith("/pause"):
            autopilot.save_settings({"enabled": False}); send_text("Autopilot paused.", chat)
        elif text.startswith("/resume"):
            autopilot.save_settings({"enabled": True}); send_text("Autopilot on.", chat)
        elif text.startswith("/post ") or "youtube.com/" in text or "youtu.be/" in text:
            url = text.replace("/post", "").strip()
            pid = autopilot.start_project_from_url(url)
            send_text(f"Started. I'll send the clips when they're ready.\n{base_url}/project/{pid}", chat)
        elif text.startswith("/help"):
            send_text("/status – what's going on\n/post <youtube link> – clip an episode now\n/pause /resume – autopilot", chat)
        return
    if "callback_query" in u:
        q = u["callback_query"]
        data = q.get("data", "")
        action, _, clip_id = data.partition(":")
        try:
            if action == "post":
                p = autopilot.queue_post(clip_id)
                _call("answerCallbackQuery", callback_query_id=q["id"], text=f"Queued for {_fmt_time(p['publish_at'])}" if p else "Could not queue")
            elif action == "skip":
                autopilot.skip_clip(clip_id)
                _call("answerCallbackQuery", callback_query_id=q["id"], text="Skipped")
            elif action == "cancel":
                autopilot.cancel_post(clip_id)
                _call("answerCallbackQuery", callback_query_id=q["id"], text="Cancelled")
        except Exception as e:  # noqa: BLE001
            db.log_error("telegram", str(e))
            _call("answerCallbackQuery", callback_query_id=q["id"], text="Something went wrong")
        refresh_clip_message(clip_id, base_url)


def run_poller(base_url_fn):
    """Long-poll Telegram for button taps and commands. Runs forever in a thread."""
    offset = int(db.get_setting("telegram_offset", 0) or 0)
    said_hello = False
    while True:
        if not enabled():
            # no token yet: wait quietly and pick one up within seconds of it being added
            state["running"] = False
            time.sleep(2)
            continue
        state["running"] = True
        if not said_hello:
            whoami()
            said_hello = True
        out = _call("getUpdates", timeout=70, offset=offset, timeout_=None, allowed_updates=["message", "callback_query"])
        if not out.get("ok"):
            # back off a little, but recover quickly when Telegram comes back
            state["backoff"] = min(10, state.get("backoff", 0) + 2)
            time.sleep(state["backoff"])
            continue
        state["backoff"] = 0
        for u in out.get("result", []):
            offset = u["update_id"] + 1
            state["updates"] += 1
            try:
                handle_update(u, base_url_fn())
            except Exception as e:  # noqa: BLE001
                db.log_error("telegram", str(e))
        db.set_setting("telegram_offset", offset)


def start_poller(base_url_fn):
    """Always start the listener: it waits quietly until a bot token appears, so adding the token later just works."""
    if any(t.name == "telegram" for t in threading.enumerate()):
        return
    threading.Thread(target=run_poller, args=(base_url_fn,), daemon=True, name="telegram").start()


def _esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "?"
    from datetime import datetime
    from .autopilot import tz
    return datetime.fromtimestamp(ts, tz()).strftime("%a %d %b %H:%M")
