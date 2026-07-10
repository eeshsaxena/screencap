"""``/v0/chat.answer`` daemon read-verb contract tests (U5).

Mirrors the read-verb harness in ``test_read_only_verbs.py`` (ASGI POST over
``httpx.ASGITransport``). The verb exposes the U3 orchestrator + U4 dispatch as a
FAIL-SAFE, read-only surface (KTD8): a downstream miss degrades to a graceful
coverage/refusal envelope — NEVER a 500 — while a malformed *input* returns a
typed 4xx. A refusal (no evidence / on-device unavailable + no cloud consent) is
a normal 200 with ``refusal=true``.

The recall generation seam is gated on SCR-243 (on-device ``answer()`` is not in
this tree), so a real dispatch with no cloud consent legitimately refuses — the
verb must handle that as a clean 200. These tests inject the recall seam
(``_run_chat_answer`` / dispatch) so they assert the VERB wiring, not the model.
"""

from __future__ import annotations

import httpx
import pytest

import screencap
from screencap.daemon import schema


async def _asgi_post(path: str, body: dict) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


def _assert_envelope(payload: dict, *, expected_schema_version: int) -> None:
    assert payload["schema_version"] == expected_schema_version
    assert payload["daemon_version"] == screencap.__version__
    assert payload["api_schema_version"] == schema.API_SCHEMA_VERSION


def _fake_answer(monkeypatch: pytest.MonkeyPatch, *, answer, refusal, sources, kind, target):
    """Replace the daemon's ChatAnswer producer so the verb is tested in isolation.

    Builds a real :class:`ChatAnswer` (so the handler's mapping to the wire shape
    is exercised) and patches ``answer_from_bundle`` — the single seam the handler
    dispatches through — to return it. ``build_evidence_bundle`` is patched to a
    trivial empty bundle so no store/disk is touched.
    """
    from screencap.recall import dispatch, orchestrator

    coverage = orchestrator.CoverageDescriptor(
        state=(
            orchestrator.CoverageState.OK
            if not refusal
            else orchestrator.CoverageState.NO_MATCHING_MOMENTS
        ),
        note="test coverage note",
        per_stream={"content": "ok"},
    )
    bundle = orchestrator.EvidenceBundle(
        question_kind=kind,
        evidence=[],
        coverage=coverage,
        figures=None,
    )
    chat_answer = dispatch.ChatAnswer(
        answer=answer,
        sources=[
            orchestrator.EvidencePointer(
                recording=s[0], timestamp_ms=s[1], stream=orchestrator.Stream(s[2])
            )
            for s in sources
        ],
        coverage=coverage,
        target=target,
        refusal=refusal,
        question_kind=kind,
    )
    monkeypatch.setattr(
        "screencap.daemon.app.build_evidence_bundle", lambda *a, **k: bundle
    )
    monkeypatch.setattr(
        "screencap.daemon.app.answer_from_bundle", lambda *a, **k: chat_answer
    )


@pytest.mark.asyncio
async def test_chat_answer_returns_answer_sources_and_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap.recall.orchestrator import QuestionKind, Stream  # noqa: F401
    from screencap.segmentation.consent import ExecutionTarget

    _fake_answer(
        monkeypatch,
        answer="You hit a refund error in the vendor portal at 2:03pm.",
        refusal=False,
        sources=[("demo", 125_000, "content")],
        kind=QuestionKind.POINT,
        target=ExecutionTarget.ON_DEVICE,
    )

    response = await _asgi_post(
        "/v0/chat.answer", {"question": "what was that refund error?"}
    )

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._CHAT_ANSWER_API_VERSION)
    assert payload["ok"] is True
    assert payload["refusal"] is False
    assert payload["answer"].startswith("You hit a refund error")
    assert payload["question_kind"] == "point"
    assert payload["target"] == "on_device"
    # Sources are POINTER-ONLY: exactly recording + timestamp_ms + stream.
    assert len(payload["sources"]) == 1
    src = payload["sources"][0]
    assert set(src) == {"recording", "timestamp_ms", "stream"}
    assert src == {"recording": "demo", "timestamp_ms": 125_000, "stream": "content"}
    # Coverage descriptor is passed through honestly.
    assert payload["coverage"]["state"] == "ok"
    assert "per_stream" in payload["coverage"]
    # Pointer-only across the WHOLE serialized body: no media path / image bytes.
    assert ".mp4" not in response.text
    assert ".jpg" not in response.text
    assert "screenshots" not in response.text


@pytest.mark.asyncio
async def test_chat_answer_no_evidence_returns_refusal_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-evidence question is a normal 200 with ``refusal=true`` + empty
    sources — NOT an error (KTD8). Covers AE1."""
    from screencap.recall.dispatch import REFUSAL_TEXT
    from screencap.recall.orchestrator import QuestionKind
    from screencap.segmentation.consent import ExecutionTarget

    _fake_answer(
        monkeypatch,
        answer=REFUSAL_TEXT,
        refusal=True,
        sources=[],
        kind=QuestionKind.POINT,
        target=ExecutionTarget.NONE,
    )

    response = await _asgi_post(
        "/v0/chat.answer", {"question": "what did I never record?"}
    )

    assert response.status_code == 200
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._CHAT_ANSWER_API_VERSION)
    assert payload["ok"] is True
    assert payload["refusal"] is True
    assert payload["sources"] == []
    assert payload["answer"] == REFUSAL_TEXT


@pytest.mark.asyncio
async def test_chat_answer_malformed_request_is_typed_4xx() -> None:
    """A malformed input body → typed 400 ``invalid_request`` (not a 500)."""
    # Missing the required ``question`` field.
    r1 = await _asgi_post("/v0/chat.answer", {"prior_turns": []})
    assert r1.status_code == 400
    assert r1.json()["error"] == "invalid_request"
    assert r1.json()["ok"] is False

    # A prior-turn pointer missing its required ``timestamp_ms``.
    r2 = await _asgi_post(
        "/v0/chat.answer",
        {"question": "hi", "prior_turns": [{"recording": "demo"}]},
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_request"

    # FIX F: an out-of-bounds ``window_ms`` element → typed 400 at the boundary,
    # never an unbounded scan into aggregate_window.
    r3 = await _asgi_post(
        "/v0/chat.answer",
        {"question": "recap", "window_ms": [0, 10**18]},
    )
    assert r3.status_code == 400
    assert r3.json()["error"] == "invalid_request"

    r4 = await _asgi_post(
        "/v0/chat.answer",
        {"question": "recap", "window_ms": [-1, 1000]},
    )
    assert r4.status_code == 400
    assert r4.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_chat_answer_downstream_failure_is_graceful_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downstream miss (orchestrator/dispatch raising) degrades to a graceful
    refusal envelope, NEVER a 500 (KTD8 fail-safe)."""

    def _boom(*a, **k):
        raise RuntimeError("retrieval backend exploded")

    # Patch the evidence-bundle builder to raise — the handler must catch it and
    # return a graceful 200 refusal envelope rather than a 500.
    monkeypatch.setattr("screencap.daemon.app.build_evidence_bundle", _boom)

    response = await _asgi_post(
        "/v0/chat.answer", {"question": "trigger a downstream failure"}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_envelope(payload, expected_schema_version=schema._CHAT_ANSWER_API_VERSION)
    assert payload["ok"] is True
    assert payload["refusal"] is True
    assert payload["sources"] == []
    # The graceful envelope carries an honest coverage state, not a leaked error.
    assert "retrieval backend exploded" not in response.text
    assert payload["coverage"]["state"] in {
        "no_matching_moments",
        "store_unavailable",
    }


@pytest.mark.asyncio
async def test_chat_answer_not_in_activity_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/v0/chat.answer`` is a read verb — NOT in ``_ACTIVITY_PATHS``, so it must
    not reset the idle-shutdown clock (a chat turn must never pin an
    auto-spawned daemon)."""
    from screencap.daemon import _idle_shutdown
    from screencap.daemon.app import build_app
    from screencap.recall.dispatch import REFUSAL_TEXT
    from screencap.recall.orchestrator import QuestionKind
    from screencap.segmentation.consent import ExecutionTarget

    # The path is statically excluded from the activity set.
    assert "/v0/chat.answer" not in _idle_shutdown._ACTIVITY_PATHS

    _fake_answer(
        monkeypatch,
        answer=REFUSAL_TEXT,
        refusal=True,
        sources=[],
        kind=QuestionKind.POINT,
        target=ExecutionTarget.NONE,
    )

    app = build_app()
    _idle_shutdown.attach(app, idle_seconds=600.0)
    sentinel = 12345.0
    app.state.idle_last_activity = sentinel

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v0/chat.answer", json={"question": "anything"})
    assert resp.status_code == 200
    # The activity middleware left the idle clock untouched.
    assert app.state.idle_last_activity == sentinel


@pytest.mark.asyncio
async def test_chat_answer_forwards_prior_turns_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler forwards prior-turn POINTERS + an aggregate window into the
    orchestrator (R14 context; R5 aggregate). Client-supplied prior prose is
    re-derived server-side by U3 — the handler passes only the typed pointers."""
    from screencap.recall.dispatch import REFUSAL_TEXT
    from screencap.recall.orchestrator import PriorTurnPointer, QuestionKind
    from screencap.segmentation.consent import ExecutionTarget

    captured: dict = {}

    from screencap.recall import orchestrator

    coverage = orchestrator.CoverageDescriptor(
        state=orchestrator.CoverageState.NO_MATCHING_MOMENTS,
        note="n",
        per_stream={},
    )
    bundle = orchestrator.EvidenceBundle(
        question_kind=QuestionKind.AGGREGATE,
        evidence=[],
        coverage=coverage,
        figures=None,
    )

    def _capture_build(question, **kwargs):
        captured["question"] = question
        captured["kwargs"] = kwargs
        return bundle

    from screencap.recall import dispatch

    chat_answer = dispatch.ChatAnswer(
        answer=REFUSAL_TEXT,
        sources=[],
        coverage=coverage,
        target=ExecutionTarget.NONE,
        refusal=True,
        question_kind=QuestionKind.AGGREGATE,
    )
    monkeypatch.setattr("screencap.daemon.app.build_evidence_bundle", _capture_build)
    monkeypatch.setattr(
        "screencap.daemon.app.answer_from_bundle", lambda *a, **k: chat_answer
    )

    response = await _asgi_post(
        "/v0/chat.answer",
        {
            "question": "how much time in Salesforce this morning?",
            "prior_turns": [
                {"recording": "demo", "timestamp_ms": 42, "stream": "content"}
            ],
            "window_ms": [1000, 5000],
            "app": "salesforce",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    assert captured["question"] == "how much time in Salesforce this morning?"
    kwargs = captured["kwargs"]
    # prior_turns arrive as typed PriorTurnPointer objects.
    priors = list(kwargs["prior_turns"])
    assert len(priors) == 1
    assert isinstance(priors[0], PriorTurnPointer)
    assert priors[0].recording == "demo"
    assert priors[0].timestamp_ms == 42
    # window_ms arrives as a (start, end) tuple for the aggregate path.
    assert tuple(kwargs["window_ms"]) == (1000, 5000)
    assert kwargs["app"] == "salesforce"
    assert kwargs["limit"] == 10
