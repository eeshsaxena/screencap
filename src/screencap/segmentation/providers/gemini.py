"""Gemini Flash backend for the segmentation provider interface.

Reimplements the Cloud Run processor's original ``_call_gemini`` / prompt /
validation flow (``scripts/process-recording/main.py``) as one backend behind
:class:`screencap.segmentation.provider.LLMProvider`. Model ``gemini-2.5-flash``,
structured output via ``_RESPONSE_SCHEMA``, temperature 0.1, JSON mime.

**BYO-key Gemini (U3 reconcile, R10).** This is the single BYO-key Gemini entry.
The API key is read from the shared Keychain via
:func:`screencap.segmentation.secrets.load_key` at call time — never held at
launch, never uploaded (R3). ``GOOGLE_GENAI_API_KEY`` remains only as a **dev
override** (never populated from the stored secret): the Keychain value wins, and
the env var is a fallback for a dev/CI harness that has no Keychain entry. The key
is passed to the SDK **explicitly** (``api_key=...``) so a Keychain secret never
rides through the process env.

**Cloud-free at import time.** ``google.genai`` is imported *lazily inside*
:meth:`GeminiProvider._call_gemini`, so importing this module never pulls the
Gemini SDK — the interface stays light for the daemon.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

#: The BYO-key vendor id for Gemini (its Keychain-store key, U2). Also the config
#: ``cloud_provider`` value that maps to this backend (R10 — one Gemini entry).
_VENDOR = "gemini"

from screencap.segmentation.generation import Evidence
from screencap.segmentation.generation_finish import (
    build_answer_prompt,
    evidence_gate_ok,
    sanitize_answer,
)
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable
from screencap.segmentation.schema import _RESPONSE_SCHEMA
from screencap.segmentation.validate import validate_llm_tasks

log = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"


def _load_gemini_key() -> str | None:
    """Return the Gemini API key: stored Keychain secret first, dev env override.

    The BYO key lives in the shared Keychain (U2); it is read lazily here, only
    when a live Gemini call is engaged. ``GOOGLE_GENAI_API_KEY`` is a **dev-only
    override** (never populated from the stored secret) — a fallback for a
    dev/CI harness without a Keychain entry. Never logs the key.
    """
    from screencap.segmentation import secrets

    try:
        key = secrets.load_key(_VENDOR)
    except Exception:  # noqa: BLE001 — a broken/locked Keychain must not crash the call
        log.debug("Gemini key read from Keychain failed; trying dev env override",
                  exc_info=True)
        key = None
    if key:
        return key
    return os.environ.get("GOOGLE_GENAI_API_KEY") or None

# Prompt lifted verbatim from the Cloud Run processor so cloud output is
# unchanged when the LLM call is routed through this backend.
_LLM_PROMPT = """\
You are a productivity analyst examining a computer activity timeline from a screen recording.

ACTIVITY LOG:
{activity_json}

INSTRUCTIONS:
1. Identify the distinct TASKS the user performed. A task is a coherent unit of work —
   not just "used an app" but "what were they trying to accomplish?"
2. Brief app switches (< 30s) mid-task are NOT separate tasks — absorb them.
3. Related activities across different apps are ONE task
   (e.g., "code in VSCode → test in Terminal → check docs in Chrome" = one dev task).
4. Use transcript speech to understand INTENT — "let me check my email" signals a task switch.

For each task return:
- start_time: relative timestamp (matching timeline format, e.g. "0:02:00")
- end_time: relative timestamp
- name: 2-3 words capturing the core task (NOT the app name)
  Good: "Fix login", "Draft roadmap", "Deploy hotfix", "Review PR", "Write tests"
  Bad: "Used VSCode", "Chrome session", "Terminal work", "Coding task"
- description: 3-5 sentences covering what was being worked on, specific actions taken,
  outcomes or blockers encountered, and tools/files involved.
  Be concrete — mention file names, URLs, error messages, or people when visible.
- category: one of [development, communication, research, admin, creative, other]
- apps_used: list of apps involved
- confidence: high | medium | low

Also provide a SESSION SUMMARY:
- overview: 4-6 sentence description of what the user accomplished, including specific
  outcomes, tools used, and any notable blockers or achievements
- primary_focus: the main category of work
- time_breakdown: approximate percentage per category
- key_accomplishments: 2-4 bullet points of specific things completed

Also provide TAGS for the entire recording session:
- tags: 3-8 lowercase hyphenated labels describing the session
  (e.g., "python", "debugging", "email-triage", "code-review", "api-design")
- Capture: languages, frameworks, tools, activity types, and domains
- Use only lowercase letters, numbers, and hyphens

RULES:
- Every second of the recording must be covered by exactly one task (no gaps, no overlaps)
- Name tasks by INTENT not by app name
- A task should be at least 1 minute long
- start_time of first task must be "0:00:00"

Return ONLY valid JSON: {{"tasks": [...], "summary": {{...}}, "tags": [...]}}"""


class GeminiProvider:
    """Google Gemini Flash segmentation backend.

    ``raw_call`` overrides how the prompt reaches a model — it takes the prompt
    string and returns the model's parsed-JSON dict (or ``None`` on failure).
    It defaults to :meth:`_call_gemini` (a live Gemini call). Injecting it lets
    the cloud processor route the raw call through its own mock seam without
    changing this backend, and lets tests exercise the format→validate flow on
    a fixed model response.
    """

    def __init__(
        self,
        raw_call: Callable[[str], dict | None] | None = None,
        answer_raw_call: Callable[[str], str | None] | None = None,
    ) -> None:
        self._raw_call = raw_call if raw_call is not None else self._call_gemini
        self._answer_raw_call = (
            answer_raw_call if answer_raw_call is not None else self._answer_gemini
        )

    def segment(self, activity_summary: dict) -> dict | None:
        """Segment the session into named tasks via Gemini.

        ``activity_summary`` is the full activity-data dict from
        ``build_activity_summary`` (keys ``summary`` / ``session_start`` /
        ``session_end`` / ``time_map``). Returns the **validated** tasks dict,
        or ``None`` if the model is unavailable/fails or its output does not
        validate. Never raises for an ordinary model/API failure.
        """
        prompt = _LLM_PROMPT.format(
            activity_json=json.dumps(activity_summary["summary"], indent=2),
        )
        raw = self._raw_call(prompt)
        if raw is None:
            return None

        try:
            return validate_llm_tasks(
                raw,
                activity_summary["session_start"],
                activity_summary["session_end"],
                activity_summary["time_map"],
            )
        except Exception:
            log.warning("Gemini output failed validation", exc_info=True)
            return None

    def _call_gemini(self, prompt: str) -> dict | None:
        """Call Gemini Flash via the Google AI API. Returns parsed JSON or None.

        ``google.genai`` is imported lazily here so this module stays cloud-free
        at import time.
        """
        try:
            from google import genai
            from google.genai import types

            api_key = _load_gemini_key()
            if not api_key:
                log.info("No Gemini API key stored, skipping Gemini")
                return None

            # Pass the key explicitly so a Keychain secret never rides the env.
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=0.1,
                ),
            )

            result = json.loads(response.text)
            log.info("Gemini Flash returned %d tasks", len(result.get("tasks", [])))
            return result

        except ImportError:
            log.info("google-genai not installed, skipping Gemini")
            return None
        except Exception:
            log.warning("Gemini call failed", exc_info=True)
            return None

    # -- Free-form generation path (SCR-243, U5) ---------------------------

    def answer(self, prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
        """Answer ``prompt`` grounded in ``evidence`` via a free-form Gemini call.

        Unlike :meth:`segment`, this uses NO structured response schema. Returns
        the sanitized answer string, or :data:`PROVIDER_UNAVAILABLE` when the
        model is unavailable/fails or produces empty output. Never raises.
        """
        # Single fail-closed gate: stripped marker (R11), str text/prompt (R12),
        # within the size caps (KTD10). The cloud path is the only off-box
        # egress, so it self-caps here rather than trusting the dispatcher.
        if not evidence_gate_ok(prompt, evidence):
            log.warning("GeminiProvider.answer refused the request (gate); unavailable")
            return PROVIDER_UNAVAILABLE

        raw = self._answer_raw_call(build_answer_prompt(prompt, evidence))
        if raw is None:
            return PROVIDER_UNAVAILABLE
        cleaned = sanitize_answer(raw)
        if not cleaned.strip():
            return PROVIDER_UNAVAILABLE
        return cleaned

    def _answer_gemini(self, prompt: str) -> str | None:
        """Free-form Gemini call (no response schema). Returns text or None.

        ``google.genai`` is imported lazily here so this module stays cloud-free
        at import time.
        """
        try:
            from google import genai
            from google.genai import types

            api_key = _load_gemini_key()
            if not api_key:
                log.info("No Gemini API key stored, skipping Gemini answer")
                return None

            # Pass the key explicitly so a Keychain secret never rides the env.
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            )
            return response.text
        except ImportError:
            log.info("google-genai not installed, skipping Gemini answer")
            return None
        except Exception:
            log.warning("Gemini answer call failed", exc_info=True)
            return None
