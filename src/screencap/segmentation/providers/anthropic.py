"""Anthropic API-key backend for the provider interface (BYO cloud, U3).

Powers a user's cloud-eligible tasks (SUMMARY, RECALL_ANSWER / Chat) with their
own Anthropic account via a **pasted API key** (R1/R2). The key is stored as a
Keychain secret (U2) and read via :func:`screencap.segmentation.secrets.load_key`
at call time — never held at launch, never in argv, never uploaded (R3).

Thin HTTP, no SDK (KTD, plan U3)
--------------------------------
The backend talks to Anthropic's **Messages** endpoint over plain HTTPS with
``requests`` imported lazily inside the call — no ``anthropic`` SDK, so importing
this module stays cloud-free / import-light. The request is pinned to the
**hardcoded** ``https://api.anthropic.com`` host with TLS verification on and NO
env/config host override, so a plaintext-readable key can only ever reach the
fixed vendor host. Auth rides the ``x-api-key`` + ``anthropic-version`` headers
(never the URL).

Privacy (KTD4, R7/R8)
---------------------
The backend receives ONLY text — the ALLOW-only ``activity_summary`` dict
(``segment``) or ``Evidence.text`` (``answer``). It is never handed, and cannot
request, frame bytes. ``segment`` fail-closes on any summary not marked
``stripped=True`` WITHOUT making a request; ``answer`` fail-closes via the shared
:func:`evidence_gate_ok`.

Return contract (see :mod:`screencap.segmentation.provider`)
------------------------------------------------------------
- a validated **tasks dict** — the call ran and its output validated (``segment``).
- ``None`` — ran but produced no usable tasks (``segment`` only).
- :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` — could not run at
  all: missing key, auth failure (401/403), network error, or unparseable output.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from screencap.segmentation.generation import Evidence
from screencap.segmentation.generation_finish import (
    build_answer_prompt,
    evidence_gate_ok,
    sanitize_answer,
)
from screencap.segmentation.local_finish import build_local_prompt, finalize_local_result
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable

log = logging.getLogger(__name__)

#: The BYO-key vendor id this backend serves (its Keychain-store key, U2).
_VENDOR = "anthropic"

#: Pinned Messages endpoint. HTTPS + hardcoded host, TLS verification ON, NO
#: env/config host override — a plaintext-readable key can only ever reach the
#: fixed vendor host.
_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

#: The Anthropic API version pin (same as the U2 validation call).
_ANTHROPIC_VERSION = "2023-06-01"

_MODEL = "claude-sonnet-4-5"

# A bounded output — the segmentation / answer replies are small; this also caps
# what a runaway response can bill / return.
_MAX_TOKENS = 4096

_REQUEST_TIMEOUT_S = 120.0

# Cap the response body a (hostile / runaway) endpoint can return.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# The Messages API needs the JSON-only instruction in the prompt rather than a
# response_format flag; the shared segmentation prompt already ends with
# "Return ONLY valid JSON", so no extra framing is required here.


class AnthropicProvider:
    """Anthropic Messages BYO-key backend. See module docs.

    ``raw_call`` / ``answer_raw_call`` are injectable seams (both default to a
    live HTTPS call with a lazy ``requests`` import) so tests exercise the
    format→validate / sanitize flow without a live vendor call. Stateless; safe
    to construct per call.
    """

    def __init__(
        self,
        raw_call: Callable[[str], dict | None] | None = None,
        answer_raw_call: Callable[[str], str | None] | None = None,
    ) -> None:
        self._raw_call = raw_call if raw_call is not None else self._call_anthropic
        self._answer_raw_call = (
            answer_raw_call if answer_raw_call is not None else self._answer_anthropic
        )

    # -- Segmentation ------------------------------------------------------

    def segment(self, activity_summary: dict) -> dict | None | ProviderUnavailable:
        """Segment the session by calling Anthropic Messages.

        See :mod:`screencap.segmentation.provider` for the tri-state return.
        Never raises for an ordinary API failure.
        """
        # Fail-closed privacy gate (R7/R8): refuse anything not explicitly marked
        # privacy-stripped. Do NOT make a request on unmarked input.
        if activity_summary.get("stripped") is not True:
            log.warning(
                "AnthropicProvider refused an activity summary not marked "
                "stripped=True (fail-closed); unavailable."
            )
            return PROVIDER_UNAVAILABLE

        try:
            prompt = build_local_prompt(activity_summary["summary"])
        except Exception:  # pragma: no cover - defensive
            log.warning("AnthropicProvider: failed to build the segmentation prompt",
                        exc_info=True)
            return PROVIDER_UNAVAILABLE

        raw = self._raw_call(prompt)
        if raw is None:
            return PROVIDER_UNAVAILABLE

        # Validate → sanitize (KTD12) → confidence-gate (KTD9), shared with the
        # downloaded / local-server / CLI backends.
        return finalize_local_result(raw, activity_summary)

    def _call_anthropic(self, prompt: str) -> dict | None:
        """Live Anthropic Messages call. Returns parsed JSON tasks or None.

        Reads the key from the Keychain (U2) at call time; a missing key returns
        ``None``. ``requests`` imported lazily. The model returns JSON as its text
        content (the shared prompt ends with "Return ONLY valid JSON"). Any
        auth/network/parse error → ``None``.
        """
        from screencap.segmentation import secrets

        api_key = secrets.load_key(_VENDOR)
        if not api_key:
            log.info("No Anthropic API key stored; skipping Anthropic")
            return None

        text = _post_messages(api_key, prompt, temperature=0.1)
        if text is None:
            return None
        payload = _strip_code_fence(text)
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            log.warning("Anthropic response content was not JSON; unavailable")
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- Free-form generation ----------------------------------------------

    def answer(self, prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
        """Answer ``prompt`` grounded in ``evidence`` via a free-form Anthropic call.

        Returns the sanitized answer string, or :data:`PROVIDER_UNAVAILABLE` when
        the model is unavailable/fails or produces empty output. Never raises.
        """
        # Single fail-closed gate: stripped marker (R7), str text/prompt (R7),
        # within the size caps (KTD10). No request on refusal.
        if not evidence_gate_ok(prompt, evidence):
            log.warning("AnthropicProvider.answer refused the request (gate); unavailable")
            return PROVIDER_UNAVAILABLE

        raw = self._answer_raw_call(build_answer_prompt(prompt, evidence))
        if raw is None:
            return PROVIDER_UNAVAILABLE
        cleaned = sanitize_answer(raw)
        if not cleaned.strip():
            return PROVIDER_UNAVAILABLE
        return cleaned

    def _answer_anthropic(self, prompt: str) -> str | None:
        """Free-form Anthropic Messages call. Returns text or None.

        Reads the key from the Keychain at call time; a missing key returns
        ``None``. ``requests`` imported lazily. Any error → ``None``.
        """
        from screencap.segmentation import secrets

        api_key = secrets.load_key(_VENDOR)
        if not api_key:
            log.info("No Anthropic API key stored; skipping Anthropic answer")
            return None

        return _post_messages(api_key, prompt, temperature=0.2)


def _post_messages(api_key: str, prompt: str, *, temperature: float) -> str | None:
    """POST a single-user-turn Messages request; return the assembled text.

    The key travels only in the ``x-api-key`` header (never the URL). Pinned to
    the hardcoded HTTPS host with TLS verification on and no host override.
    Streams the body under a size cap. Returns the concatenated text blocks of the
    assistant reply, or ``None`` on any auth/network/parse failure. Never raises,
    never logs the key.
    """
    import requests

    body = {
        "model": _MODEL,
        "max_tokens": _MAX_TOKENS,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        with requests.post(
            _MESSAGES_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
            timeout=_REQUEST_TIMEOUT_S,
            allow_redirects=False,  # a 302 must not bounce us off the pinned host
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                # 401/403 (bad key) and everything else are ordinary failures →
                # None → PROVIDER_UNAVAILABLE. Only the status class is logged.
                log.info("Anthropic call returned HTTP %s; unavailable", resp.status_code)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    log.warning("Anthropic response exceeds the size cap; unavailable")
                    return None
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        # Messages returns a list of content blocks; concatenate the text ones.
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            return None
        text = "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return text or None
    except Exception:  # noqa: BLE001 — any transport/parse error is unavailable, not fatal
        log.warning("Anthropic call failed", exc_info=True)
        return None


def _strip_code_fence(text: str) -> str:
    """Return ``text`` with a leading/trailing Markdown code fence removed.

    A model often wraps a JSON reply in a ```` ```json … ``` ```` block; strip a
    single outer fence so the JSON parses. A plain (unfenced) reply is returned
    unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()
