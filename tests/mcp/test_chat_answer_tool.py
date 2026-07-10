"""Tests for the U6 ``chat_answer`` MCP tool (R15, KTD9).

The tool is a THIN forward to the daemon's ``/v0/chat.answer`` verb (U5),
re-wrapping the envelope as a typed, POINTER-ONLY result (answer prose + source
``(recording, timestamp_ms, stream)`` pointers, coverage, refusal, question_kind).
No image bytes, no file paths.

This is the first MCP surface returning model-generated PROSE, so the answer text
is run through the same control-char/markup OUTPUT sanitizer applied to
model-emitted task names (``segmentation.sanitize._clean_text``) before it reaches
the agent — an evidence-influenced answer can carry injected markup / control
sequences aimed at the downstream consumer.

Mirrors the stub-client harness in ``tests/test_mcp_server.py``.
"""

from __future__ import annotations

import pytest

from screencap.mcp import server
from screencap.mcp._client import DaemonError

# --------------------------------------------------------------------------
# Stub client (mirrors tests/test_mcp_server.py::_StubClient shape)
# --------------------------------------------------------------------------


class _StubClient:
    def __init__(self, response: dict | None = None, *, raises: Exception | None = None):
        self.response = response or {}
        self.raises = raises
        self.calls: list[tuple] = []

    async def chat_answer(
        self, question, *, prior_turns=None, window_ms=None, app=None, limit=None,
    ):
        self.calls.append(("chat", question, prior_turns, window_ms, app, limit))
        if self.raises is not None:
            raise self.raises
        return self.response


def _use_client(monkeypatch, client) -> None:
    async def _fake_client():
        return client

    monkeypatch.setattr(server, "_client", _fake_client)


_UNSET = object()


def _envelope(answer: str, *, sources=_UNSET, refusal=False, kind="point") -> dict:
    if sources is _UNSET:
        sources = [{"recording": "demo", "timestamp_ms": 125000, "stream": "content"}]
    return {
        "ok": True,
        "answer": answer,
        "sources": sources,
        "coverage": {"state": "ok", "note": "covered", "per_stream": {"content": "ok"}},
        "refusal": refusal,
        "question_kind": kind,
        "target": "on_device",
    }


# --------------------------------------------------------------------------
# Forwarding + pointer-only mapping
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_answer_forwards_and_maps_pointer_sources(monkeypatch):
    stub = _StubClient(_envelope("You hit a refund error in the vendor portal."))
    _use_client(monkeypatch, stub)

    result = await server.chat_answer("what was that refund error?")

    assert isinstance(result, server.ChatAnswerResult)
    # Forwarded to the daemon chat.answer verb with the question.
    assert stub.calls[0][0] == "chat"
    assert stub.calls[0][1] == "what was that refund error?"
    # Answer prose + pointer sources come back.
    assert "refund error" in result.answer
    assert result.sources[0].recording == "demo"
    assert result.sources[0].timestamp_ms == 125000
    assert result.sources[0].stream == "content"
    assert result.coverage.state == "ok"
    assert result.refusal is False
    assert result.question_kind == "point"
    # FIX G: the execution target the daemon carries is surfaced on the result.
    assert result.target == "on_device"


@pytest.mark.asyncio
async def test_chat_answer_result_is_pointer_only(monkeypatch):
    """POINTER-ONLY (R15): the source model has no path/bytes field by construction —
    a leaked media path can't ride through the typed result."""
    stub = _StubClient(_envelope("answer"))
    _use_client(monkeypatch, stub)

    result = await server.chat_answer("q")

    # The source model carries ONLY the (recording, timestamp_ms, stream) pointer.
    assert set(server.ChatSource.model_fields) == {"recording", "timestamp_ms", "stream"}
    dumped = result.sources[0].model_dump()
    assert "path" not in dumped
    assert "image" not in dumped and "bytes" not in dumped and "jpg" not in dumped


@pytest.mark.asyncio
async def test_chat_answer_target_defaults_to_none_when_absent(monkeypatch):
    """FIX G: an older daemon that omits ``target`` decodes to the safe "none"
    default rather than dropping the field or erroring."""
    env = _envelope("answer")
    del env["target"]
    stub = _StubClient(env)
    _use_client(monkeypatch, stub)

    result = await server.chat_answer("q")
    assert result.target == "none"


@pytest.mark.asyncio
async def test_chat_answer_forwards_optional_args(monkeypatch):
    stub = _StubClient(_envelope("aggregate answer", kind="aggregate"))
    _use_client(monkeypatch, stub)

    prior = [{"recording": "demo", "timestamp_ms": 5000, "stream": "content"}]
    await server.chat_answer(
        "how much time in Salesforce?",
        prior_turns=prior,
        window_ms=(1000, 9000),
        app="Salesforce",
        limit=25,
    )

    _, _q, fwd_prior, fwd_window, fwd_app, fwd_limit = stub.calls[0]
    assert fwd_prior == prior
    assert fwd_window == (1000, 9000)
    assert fwd_app == "Salesforce"
    assert fwd_limit == 25


# --------------------------------------------------------------------------
# Output sanitization (first MCP surface returning model prose)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_answer_prose_is_output_sanitized(monkeypatch):
    """Injected markup + control sequences in the model answer are stripped before
    the agent sees them (the same hardening applied to model-emitted task names)."""
    dirty = "Total <script>alert(1)</script> is\x07 <b>42</b>\x00 done"
    stub = _StubClient(_envelope(dirty))
    _use_client(monkeypatch, stub)

    result = await server.chat_answer("q")

    # Angle-bracket markup spans are removed.
    assert "<script>" not in result.answer
    assert "</script>" not in result.answer
    assert "<b>" not in result.answer
    # Control characters are stripped.
    assert "\x07" not in result.answer
    assert "\x00" not in result.answer
    # The legible words survive.
    assert "Total" in result.answer
    assert "42" in result.answer
    assert "done" in result.answer


# --------------------------------------------------------------------------
# Refusal + daemon-down
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_answer_refusal_surfaces_intact(monkeypatch):
    """A no-evidence refusal is a normal result (refusal=True + empty sources),
    not an error."""
    stub = _StubClient(
        _envelope("I don't have that in your history.", sources=[], refusal=True)
    )
    _use_client(monkeypatch, stub)

    result = await server.chat_answer("something not recorded")

    assert result.refusal is True
    assert result.sources == []
    assert "don't have that" in result.answer


@pytest.mark.asyncio
async def test_chat_answer_daemon_unreachable_raises_clean_error(monkeypatch):
    """Daemon-down surfaces the typed DaemonError, not a crash."""
    stub = _StubClient(raises=DaemonError("daemon unreachable"))
    _use_client(monkeypatch, stub)

    with pytest.raises(DaemonError):
        await server.chat_answer("q")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_answer_is_registered():
    mcp = server.build_server()
    names = {t.name for t in await mcp.list_tools()}
    assert "chat_answer" in names
