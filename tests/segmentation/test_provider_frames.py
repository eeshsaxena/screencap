"""Provider multimodal capability + graceful omission (SCR-272, U4).

Pins the ``supports_frames`` capability signal and the Gemini multimodal path:

- ``supports_frames`` defaults **False** for every existing backend and is **True**
  only for the vision-capable Gemini backend.
- Gemini, given masked frames AND ``supports_frames`` True, builds a multimodal
  ``contents`` payload — ``[prompt, Part.from_bytes(...), ...]`` — for BOTH the
  ``segment`` and ``answer`` entry points; with no frames it sends a text-only
  ``contents`` (a bare prompt str, unchanged from today); with the flag forced
  False it stays text-only even when frames are handed in.
- A non-vision backend given ``masked_frames`` simply ignores them and sends
  text — it never raises (graceful omission).

The Gemini tests inject a stub ``google.genai`` (no network, Vision-free) that
records the ``contents`` handed to ``generate_content``. Test files are exempt
from the ``MaskedFrame(masked=True)`` builder-only AST guard (it scans ``src/``
only), so these tests may mint masked frames directly.
"""

from __future__ import annotations

import json
import types as _pytypes

import pytest

from screencap.segmentation.generation import Evidence, MaskedFrame
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE
from screencap.segmentation.providers.gemini import GeminiProvider

# The whole file exercises the frames-to-cloud egress boundary (what image bytes
# leave, and that non-vision backends never leak frames), so it is privacy-bearing
# — mark the module so CI's ``-m privacy`` lane runs it. Vision-free + network-free
# (the google-genai client is a stub; no OCR/Vision is touched).
pytestmark = pytest.mark.privacy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _frame(data: bytes = b"jpegbytes", ts: int = 300_000) -> MaskedFrame:
    """Mint a provenance-marked masked frame (allowed in tests — the AST guard
    that pins ``frame_egress`` as the sole minter scans ``src/`` only)."""
    return MaskedFrame(jpeg_bytes=data, timestamp_ms=ts, masked=True)


def _stripped_summary() -> dict:
    return {
        "stripped": True,
        "summary": {
            "duration": "1h 0m 0s",
            "timeline": [{"time": "0:00:00", "app": "VS Code", "cat": "CODE"}],
            "transcript": [],
        },
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {"0:00:00": 1000.0, "0:30:00": 2800.0, "1:00:00": 4600.0},
    }


def _tasks_json() -> str:
    return json.dumps(
        {
            "tasks": [
                {"start_time": "0:00:00", "end_time": "0:30:00", "name": "Fix login",
                 "description": "d", "category": "development", "apps_used": ["VS Code"],
                 "confidence": "high"},
            ],
            "summary": {"overview": "o", "primary_focus": "development",
                        "time_breakdown": {}, "key_accomplishments": []},
            "tags": ["python"],
        }
    )


class _FakePart:
    """Stand-in for ``google.genai.types.Part`` inline-image parts."""

    def __init__(self, data: bytes, mime_type: str) -> None:
        self.data = data
        self.mime_type = mime_type

    @classmethod
    def from_bytes(cls, *, data: bytes, mime_type: str) -> "_FakePart":
        return cls(data=data, mime_type=mime_type)


def _inject_genai(monkeypatch, captured: dict, *, answer_text: str = "You edited main.py.") -> None:
    """Inject a stub ``google.genai`` that records the ``contents`` it is handed.

    ``generate_content`` returns a JSON tasks string (so ``segment`` completes)
    unless the recorded prompt is a free-form answer — the test reads
    ``captured['contents']`` regardless of the return value.
    """
    import sys

    class _Models:
        def generate_content(self, **kw):
            captured["contents"] = kw.get("contents")
            captured["model"] = kw.get("model")
            # ``answer`` has no response_schema in its config; segment does.
            cfg = kw.get("config") or {}
            is_answer = "response_schema" not in cfg
            return _pytypes.SimpleNamespace(
                text=answer_text if is_answer else _tasks_json()
            )

    class _Client:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.models = _Models()

    class _Types:
        Part = _FakePart

        @staticmethod
        def GenerateContentConfig(**kw):
            return kw

    gtypes = _Types()
    genai = _pytypes.SimpleNamespace(Client=_Client, types=gtypes)
    google_pkg = _pytypes.ModuleType("google")
    google_pkg.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", gtypes)

    from screencap.segmentation import secrets

    monkeypatch.setattr(secrets, "load_key", lambda vendor: "k")
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# supports_frames capability signal
# ---------------------------------------------------------------------------


class TestSupportsFramesCapability:
    def test_gemini_supports_frames_true(self):
        assert GeminiProvider().supports_frames is True

    def test_every_other_backend_defaults_false(self):
        from screencap.segmentation.providers.anthropic import AnthropicProvider
        from screencap.segmentation.providers.cli_delegate import CliDelegateProvider
        from screencap.segmentation.providers.downloaded import DownloadedProvider
        from screencap.segmentation.providers.local_server import LocalServerProvider
        from screencap.segmentation.providers.ondevice import OnDeviceProvider
        from screencap.segmentation.providers.openai import OpenAIProvider

        assert AnthropicProvider().supports_frames is False
        assert OpenAIProvider().supports_frames is False
        assert OnDeviceProvider().supports_frames is False
        assert DownloadedProvider().supports_frames is False
        assert LocalServerProvider().supports_frames is False
        assert CliDelegateProvider("anthropic-cli").supports_frames is False


# ---------------------------------------------------------------------------
# Gemini attaches image parts (segment + answer) — the multimodal payload
# ---------------------------------------------------------------------------


class TestGeminiAttachesFrames:
    def test_segment_builds_multimodal_contents(self, monkeypatch):
        captured: dict = {}
        _inject_genai(monkeypatch, captured)
        frames = (_frame(b"a", 1), _frame(b"b", 2))

        GeminiProvider().segment(_stripped_summary(), masked_frames=frames)

        contents = captured["contents"]
        assert isinstance(contents, list)
        assert isinstance(contents[0], str)  # the prompt text leads
        parts = contents[1:]
        assert [p.data for p in parts] == [b"a", b"b"]
        assert all(p.mime_type == "image/jpeg" for p in parts)

    def test_answer_builds_multimodal_contents(self, monkeypatch):
        captured: dict = {}
        _inject_genai(monkeypatch, captured)
        frames = (_frame(b"x", 7),)

        out = GeminiProvider().answer(
            "what did I do?", Evidence(text="edited main.py", stripped=True),
            masked_frames=frames,
        )
        assert isinstance(out, str)  # a grounded answer came back, no raise

        contents = captured["contents"]
        assert isinstance(contents, list)
        assert isinstance(contents[0], str)
        assert contents[1].data == b"x"
        assert contents[1].mime_type == "image/jpeg"

    def test_answer_reads_frames_off_evidence(self, monkeypatch):
        # Frames may also ride inside Evidence.masked_frames (the recall carrier);
        # Gemini attaches those too when no explicit kwarg is given.
        captured: dict = {}
        _inject_genai(monkeypatch, captured)
        ev = Evidence(text="edited main.py", stripped=True, masked_frames=(_frame(b"z", 9),))

        GeminiProvider().answer("q", ev)

        contents = captured["contents"]
        assert isinstance(contents, list)
        assert contents[1].data == b"z"


# ---------------------------------------------------------------------------
# Text-only when no frames / capability off (unchanged from today)
# ---------------------------------------------------------------------------


class TestGeminiTextOnly:
    def test_segment_no_frames_is_text_only(self, monkeypatch):
        captured: dict = {}
        _inject_genai(monkeypatch, captured)

        GeminiProvider().segment(_stripped_summary())

        assert isinstance(captured["contents"], str)  # bare prompt, no image parts

    def test_answer_no_frames_is_text_only(self, monkeypatch):
        captured: dict = {}
        _inject_genai(monkeypatch, captured)

        GeminiProvider().answer("q", Evidence(text="edited main.py", stripped=True))

        assert isinstance(captured["contents"], str)

    def test_flag_forced_false_sends_text_only(self, monkeypatch):
        # Frames are attached ONLY when supports_frames is True: a Gemini with the
        # capability forced off drops the frames and sends text.
        captured: dict = {}
        _inject_genai(monkeypatch, captured)
        provider = GeminiProvider()
        provider.supports_frames = False  # type: ignore[misc]

        provider.segment(_stripped_summary(), masked_frames=(_frame(),))

        assert isinstance(captured["contents"], str)


# ---------------------------------------------------------------------------
# Graceful omission — a non-vision backend ignores frames, never raises
# ---------------------------------------------------------------------------


class TestGracefulOmission:
    def test_non_vision_segment_ignores_frames(self):
        from screencap.segmentation.providers.anthropic import AnthropicProvider

        seen: list = []

        def _raw(prompt):
            seen.append(prompt)  # only text reaches the model — no frame bytes
            return None

        provider = AnthropicProvider(raw_call=_raw)
        # Passing masked_frames must not raise; the backend omits them.
        result = provider.segment(_stripped_summary(), masked_frames=(_frame(),))

        assert result is PROVIDER_UNAVAILABLE  # ran text-only, no usable tasks
        assert len(seen) == 1 and isinstance(seen[0], str)

    def test_non_vision_answer_ignores_frames(self):
        from screencap.segmentation.providers.anthropic import AnthropicProvider

        seen: list = []

        def _answer_raw(prompt):
            seen.append(prompt)
            return "a grounded answer"

        provider = AnthropicProvider(answer_raw_call=_answer_raw)
        out = provider.answer(
            "q", Evidence(text="edited main.py", stripped=True),
            masked_frames=(_frame(),),
        )

        assert isinstance(out, str)  # produced an answer, never raised
        assert len(seen) == 1 and isinstance(seen[0], str)
