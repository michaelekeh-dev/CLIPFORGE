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


def client():
    global _client
    with _lock:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic(max_retries=3, timeout=300)
        return _client


class LLMError(Exception):
    pass


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
            raise LLMError(str(e)) from e
    except anthropic.APIError as e:
        db.log_error("llm", str(e))
        raise LLMError(str(e)) from e
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
