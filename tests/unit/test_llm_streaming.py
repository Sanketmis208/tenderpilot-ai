"""Streaming reassembly for the OpenAI-compatible (Groq) backend — no network."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


def _sse(*chunks):
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


class _Resp:
    def __init__(self, lines, status=200):
        self.lines, self.status_code, self.headers = lines, status, {}

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return b""

    def json(self):
        return {"error": {"message": "bad"}}


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("TP_LLM_PROVIDER", "groq")
    monkeypatch.setenv("TP_GROQ_API_KEY", "test-not-a-real-key")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.ai.llm.client import GroqBackend

    yield GroqBackend()
    get_settings.cache_clear()


def _patch(monkeypatch, lines, status=200):
    import httpx

    @contextmanager
    def fake_stream(method, url, **kw):
        assert kw["json"]["stream"] is True
        yield _Resp(lines, status)

    monkeypatch.setattr(httpx, "stream", fake_stream)


def test_streamed_text_and_usage(backend, monkeypatch):
    _patch(monkeypatch, _sse(
        {"model": "openai/gpt-oss-120b", "choices": [{"delta": {"content": "Three "}}]},
        {"choices": [{"delta": {"content": "requirements are met."}}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7}},
    ))
    from app.ai.llm.client import _OpenAIToolConversation

    got: list[str] = []
    conv = _OpenAIToolConversation(backend, "sys", [{"role": "user", "content": "q"}], [])
    turn = conv.step(got.append)
    assert got == ["Three ", "requirements are met."]
    assert turn.text == "Three requirements are met." and not turn.calls
    assert (turn.usage.tokens_in, turn.usage.tokens_out) == (120, 7)


def test_streamed_tool_call_fragments_are_reassembled(backend, monkeypatch):
    _patch(monkeypatch, _sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "get_tender_", "arguments": '{"tender'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "details", "arguments": '_id": "abc"}'}}]}}]},
        {"choices": [], "x_groq": {"usage": {"prompt_tokens": 50, "completion_tokens": 9}}},
    ))
    from app.ai.llm.client import _OpenAIToolConversation

    conv = _OpenAIToolConversation(backend, "sys", [{"role": "user", "content": "q"}], [])
    turn = conv.step(lambda _t: None)
    assert [(c.id, c.name, c.args) for c in turn.calls] == [("call_1", "get_tender_details", {"tender_id": "abc"})]
    assert conv.messages[-1]["tool_calls"][0]["function"]["name"] == "get_tender_details"
    assert turn.usage.tokens_out == 9


def test_streaming_client_error_is_reported(backend, monkeypatch):
    _patch(monkeypatch, [], status=400)
    from app.ai.llm.client import _OpenAIToolConversation
    from app.core.errors import ServiceUnavailable

    with pytest.raises(ServiceUnavailable):
        _OpenAIToolConversation(backend, "sys", [{"role": "user", "content": "q"}], []).step(lambda _t: None)
