"""The Claude door: the workspace header, plain-words errors, and the key test."""
import os
import sys
import types
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clipforge import llm  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "CLIPFORGE_LLM"):
        monkeypatch.delenv(k, raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def fake_anthropic(monkeypatch, recorder):
    mod = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, **kwargs):
            recorder.update(kwargs)

    mod.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod


def test_client_sends_no_workspace_header_when_unset(monkeypatch):
    seen = {}
    fake_anthropic(monkeypatch, seen)
    llm.client()
    assert "default_headers" not in seen


def test_client_sends_the_workspace_header_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    seen = {}
    fake_anthropic(monkeypatch, seen)
    llm.client()
    assert seen["default_headers"] == {"anthropic-workspace-id": "wrkspc_123"}


def test_workspace_error_becomes_a_plain_instruction():
    msg = llm.plain("Error code: 400 - {'message': 'This API key is not scoped to a workspace, so this request must "
                    "include the anthropic-workspace-id header'}")
    assert "console.anthropic.com" in msg
    assert "ANTHROPIC_WORKSPACE_ID" in msg
    assert "anthropic-workspace-id header" not in msg


def test_workspace_error_with_an_id_set_blames_the_id(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    msg = llm.plain("This API key is not scoped to a workspace")
    assert "ANTHROPIC_WORKSPACE_ID" in msg and "refused" in msg


def test_other_errors_get_plain_words():
    assert "no credit left" in llm.plain("Your credit balance is too low")
    assert "refused the key" in llm.plain("invalid x-api-key")
    assert "rate limiting" in llm.plain("Error code: 429 rate limit exceeded")


def test_unknown_errors_pass_through_unchanged():
    assert llm.plain("something odd happened") == "something odd happened"


def test_check_says_what_to_do_with_no_key():
    out = llm.check()
    assert out["ok"] is False and "ANTHROPIC_API_KEY" in out["detail"]


def test_check_says_claude_is_switched_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLIPFORGE_LLM", "mock")
    assert llm.check()["ok"] is False
    assert "switched off" in llm.check()["detail"]


def test_check_reports_the_workspace_problem_in_plain_words(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: (_ for _ in ()).throw(
        llm.LLMError(llm.plain("This API key is not scoped to a workspace"))))
    out = llm.check()
    assert out["ok"] is False and "console.anthropic.com" in out["detail"]


def test_check_is_happy_when_claude_answers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"ok": True})
    out = llm.check()
    assert out["ok"] is True and "answered" in out["detail"]
