"""OpenAI API-key backend for the provider interface (BYO cloud, U3).

Powers a user's cloud-eligible tasks (SUMMARY, RECALL_ANSWER / Chat) with their
own OpenAI account via a **pasted API key** (R1/R2). The key is stored as a
Keychain secret (U2) and read via :func:`screencap.segmentation.secrets.load_key`
at call time — never held at launch, never in argv, never uploaded (R3).

Thin HTTP, no SDK (KTD, plan U3)
--------------------------------
The backend talks to OpenAI's **Chat Completions** endpoint over plain HTTPS with
``requests`` imported lazily inside the call — no ``openai`` SDK, so importing
this module stays cloud-free / import-light. The request is pinned to the
**hardcoded** ``https://api.openai.com`` host with TLS verification on and NO
env/config host override, so a plaintext-readable key can only ever reach the
fixed vendor host (mirrors :mod:`screencap.segmentation.secrets`'s validation
pins).

Privacy (KTD4, R7/R8)
---------------------
The backend receives ONLY text — the ALLOW-only ``activity_summary`` dict
(``segment``) or ``Evidence.text`` (``answer``). It is never handed, and cannot
request, frame bytes. ``segment`` fail-closes on any summary not marked
``stripped=True`` (mirroring the local-server / CLI backends) WITHOUT making a
request; ``answer`` fail-closes via the shared :func:`evidence_gate_ok`.

Return contract (see :mod:`screencap.segmentation.provider`)
------------------------------------------------------------
- a validated **tasks dict** — the call ran and its output validated (``segment``).
- ``None`` — ran but produced no usable tasks (``segment`` only).
- :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` — could not run at
  all: missing key, auth failure (401/403), network error, or unparseable output.
  Degradation routes on this sentinel (→ on-device / heuristic), never a raise.
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
_VENDOR = "openai"

#: Pinned Chat Completions endpoint. HTTPS + hardcoded host, TLS verification ON,
#: NO env/config host override — a plaintext-readable key can only ever reach the
#: fixed vendor host.
_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

_MODEL = "gpt-4o"

_REQUEST_TIMEOUT_S = 120.0

# Cap the response body a (hostile / runaway) endpoint can return so it can't
# drive daemon memory pressure — analogous to the local-server / CLI caps.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024


class OpenAIProvider:
    """OpenAI Chat Completions BYO-key backend. See module docs.

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
        self._raw_call = raw_call if raw_call is not None else self._call_openai
        self._answer_raw_call = (
            answer_raw_call if answer_raw_call is not None else self._answer_openai
        )

    # -- Segmentation ------------------------------------------------------

    def segment(self, activity_summary: dict) -> dict | None | ProviderUnavailable:
        """Segment the session by calling OpenAI Chat Completions.

        See :mod:`screencap.segmentation.provider` for the tri-state return.
        Never raises for an ordinary API failure.
        """
        # Fail-closed privacy gate (R7/R8): refuse anything not explicitly marked
        # privacy-stripped. Do NOT make a request on unmarked input.
        if activity_summary.get("stripped") is not True:
            log.warning(
                "OpenAIProvider refused an activity summary not marked "
                "stripped=True (fail-closed); unavailable."
            )
            return PROVIDER_UNAVAILABLE

        try:
            prompt = build_local_prompt(activity_summary["summary"])
        except Exception:  # pragma: no cover - defensive
            log.warning("OpenAIProvider: failed to build the segmentation prompt",
                        exc_info=True)
            return PROVIDER_UNAVAILABLE

        raw = self._raw_call(prompt)
        if raw is None:
            # Missing key / auth failure / network error / unparseable → could not
            # run at all → unavailable, so degradation routes to on-device.
            return PROVIDER_UNAVAILABLE

        # Validate → sanitize (KTD12) → confidence-gate (KTD9), shared with the
        # downloaded / local-server / CLI backends (a BYO cloud's names reach the
        # same sink). Returns the finished dict or ``None`` (ran, no usable output).
        return finalize_local_result(raw, activity_summary)

    def _call_openai(self, prompt: str) -> dict | None:
        """Live OpenAI Chat Completions call. Returns parsed JSON tasks or None.

        Reads the key from the Keychain (U2) at call time; a missing key returns
        ``None`` (→ unavailable). ``requests`` is imported lazily so this module
        stays cloud-free at import. Any auth/network/parse error → ``None``.
        """
        from screencap.segmentation import secrets

        api_key = secrets.load_key(_VENDOR)
        if not api_key:
            log.info("No OpenAI API key stored; skipping OpenAI")
            return None

        body = {
            "model": _MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        content = _post_chat(api_key, body)
        if content is None:
            return None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            log.warning("OpenAI response content was not JSON; unavailable")
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- Free-form generation ----------------------------------------------

    def answer(self, prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
        """Answer ``prompt`` grounded in ``evidence`` via a free-form OpenAI call.

        Returns the sanitized answer string, or :data:`PROVIDER_UNAVAILABLE` when
        the model is unavailable/fails or produces empty output. Never raises.
        """
        # Single fail-closed gate: stripped marker (R7), str text/prompt (R7),
        # within the size caps (KTD10). No request on refusal.
        if not evidence_gate_ok(prompt, evidence):
            log.warning("OpenAIProvider.answer refused the request (gate); unavailable")
            return PROVIDER_UNAVAILABLE

        raw = self._answer_raw_call(build_answer_prompt(prompt, evidence))
        if raw is None:
            return PROVIDER_UNAVAILABLE
        cleaned = sanitize_answer(raw)
        if not cleaned.strip():
            return PROVIDER_UNAVAILABLE
        return cleaned

    def _answer_openai(self, prompt: str) -> str | None:
        """Free-form OpenAI Chat Completions call (no JSON response format).

        Reads the key from the Keychain at call time; a missing key returns
        ``None``. ``requests`` imported lazily. Any error → ``None``.
        """
        from screencap.segmentation import secrets

        api_key = secrets.load_key(_VENDOR)
        if not api_key:
            log.info("No OpenAI API key stored; skipping OpenAI answer")
            return None

        body = {
            "model": _MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        return _post_chat(api_key, body)


def _post_chat(api_key: str, body: dict) -> str | None:
    """POST ``body`` to the pinned Chat Completions host; return message content.

    The key travels only in the ``Authorization`` header (never the URL). Pinned
    to the hardcoded HTTPS host with TLS verification on and no host override.
    Streams the body under a size cap so a runaway response can't drive memory
    pressure. Returns the assistant message ``content`` string, or ``None`` on any
    auth/network/parse failure. Never raises, never logs the key.
    """
    import requests

    try:
        with requests.post(
            _CHAT_COMPLETIONS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
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
                log.info("OpenAI call returned HTTP %s; unavailable", resp.status_code)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    log.warning("OpenAI response exceeds the size cap; unavailable")
                    return None
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        content = payload["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else None
    except Exception:  # noqa: BLE001 — any transport/parse error is unavailable, not fatal
        log.warning("OpenAI call failed", exc_info=True)
        return None
