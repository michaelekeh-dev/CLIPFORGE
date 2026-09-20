"""One door to Claude. Live when ANTHROPIC_API_KEY is set, otherwise callers fall back to heuristics."""
from __future__ import annotations
import json
import re
import threading
from .config import cfg, env
from . import db

_client = None
_lock = threading.Lock()


def mode() -> str:
    if env("CLIPFORGE_LLM") in ("mock", "off"):
        return "mock"
    return "live" if env("ANTHROPIC_API_KEY") else "mock"


def workspace_id() -> str:
    """Optional workspace the key belongs to. Only needed for keys that are not tied to one."""
    return env("ANTHROPIC_WORKSPACE_ID").strip()


def client():
    global _client
    with _lock:
        if _client is None:
            import anthropic
            kwargs = dict(max_retries=3, timeout=300)
            ws = workspace_id()
            if ws:
                kwargs["default_headers"] = {"anthropic-workspace-id": ws}
            _client = anthropic.Anthropic(**kwargs)
        return _client


def reset_client() -> None:
    """Forget the client so a new key or workspace is picked up without a restart."""
    global _client
    with _lock:
        _client = None


class LLMError(Exception):
    pass


def plain(err: str) -> str:
    """Turn an Anthropic error into one sentence a person can act on."""
    low = err.lower()
    if "not scoped to a workspace" in low or "anthropic-workspace-id" in low:
        if workspace_id():
            return ("Anthropic refused the workspace ID you set. Check ANTHROPIC_WORKSPACE_ID matches the workspace "
                    "the key belongs to, or make a key inside that workspace instead.")
        return ("Your Anthropic key is not tied to a workspace. Open console.anthropic.com, go to the workspace you "
                "want to use, make an API key there and put it in ANTHROPIC_API_KEY. Or keep this key and set "
                "ANTHROPIC_WORKSPACE_ID to that workspace's ID.")
    if "invalid x-api-key" in low or "authentication" in low or "401" in low:
        return "Anthropic refused the key. Copy it again from console.anthropic.com into ANTHROPIC_API_KEY."
    if "credit balance" in low or "billing" in low or "quota" in low:
        return "Your Anthropic account has no credit left. Top it up at console.anthropic.com."
    if "permission" in low or "403" in low:
        return "This Anthropic key is not allowed to use that model. Check the workspace's model access."
    if "rate limit" in low or "429" in low:
        return "Anthropic is rate limiting this key right now. It will work again in a minute."
    if "overloaded" in low or "529" in low:
        return "Anthropic is busy right now. Try again in a minute."
    if "not_found" in low or "404" in low:
        return f"Anthropic does not know that model name. Check llm.pick_model in config.yaml. ({err})"
    return err


def ask_json(prompt: str, system: str = "", model: str | None = None, schema: dict | None = None,
             max_tokens: int | None = None, effort: str = "medium") -> dict | list:
    """Ask Claude for JSON. Uses the JSON schema output format when given; parses text otherwise."""
    if mode() != "live":
        raise LLMError("no api key")
    import anthropic
    model = model or cfg.get("llm.pick_model")
    max_tokens = max_tokens or int(cfg.get("llm.max_tokens", 8000))
    kwargs = dict(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    if system:
        kwargs["system"] = system
    if not model.startswith("claude-haiku"):
        kwargs["output_config"] = {"effort": effort}
    if schema:
        kwargs.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": schema}
    try:
        with client().messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
    except anthropic.BadRequestError as e:
        # Older gateways may not know output_config; retry plain.
        if "output_config" in str(e) or "format" in str(e):
            kwargs.pop("output_config", None)
            with client().messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        else:
            db.log_error("llm", str(e))
            raise LLMError(plain(str(e))) from e
    except anthropic.APIError as e:
        db.log_error("llm", str(e))
        raise LLMError(plain(str(e))) from e
    if msg.stop_reason == "refusal":
        raise LLMError("Claude declined this request")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return parse_json(text)


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Claude did not return valid JSON")


PING_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
               "additionalProperties": False}


def check() -> dict:
    """Ask Claude one tiny question so the Status page can say whether Claude really works."""
    model = cfg.get("llm.pick_model")
    if env("CLIPFORGE_LLM") in ("mock", "off"):
        return {"ok": False, "detail": "Claude is switched off (CLIPFORGE_LLM is set to mock). Clips are picked by a keyword rule."}
    if not env("ANTHROPIC_API_KEY"):
        return {"ok": False, "detail": "No Anthropic key set. Add ANTHROPIC_API_KEY and Claude will pick the moments, titles and hooks."}
    try:
        ask_json("Reply with {\"ok\": true} and nothing else.", model=model, schema=PING_SCHEMA,
                 max_tokens=200, effort="low")
    except LLMError as e:
        return {"ok": False, "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": plain(str(e))}
    ws = workspace_id()
    return {"ok": True, "detail": f"Claude answered ({model})" + (f", workspace {ws[:8]}…" if ws else "") + "."}
