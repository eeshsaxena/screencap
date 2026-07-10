"""Bring-your-own local model-server backend (U7, R4/R5, SCR-239).

Talks to a user-run OpenAI-compatible / Ollama HTTP endpoint. Whether the endpoint
is treated as **on-device** (day-split allowed) or as a **cloud** provider is
decided by :func:`screencap.segmentation.endpoint.classify_endpoint` (loopback
literals only are LOCAL) — U8 uses that to place the provider. This backend adds
the **connect-time** half of the boundary (KTD8): it disables HTTP redirects,
pins ``localhost`` to ``127.0.0.1``, and caps the response body, so a 302 or an
off-box resolution fails closed rather than delivering the summary.

Mirrors :class:`~screencap.segmentation.providers.gemini.GeminiProvider`: an
injectable ``raw_call`` seam (lazy ``requests`` import inside the default) keeps
the module cloud-free at import and CI-testable without a live server. Validated
output runs through the same KTD12 sanitizer + KTD9 confidence gate the downloaded
provider uses (a BYO server's names reach the same ``tasks.list``/MCP sink).

Return contract: a validated **tasks dict** / ``None`` (ran, no usable output) /
:data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` (could not run —
endpoint unset, unreachable, timeout, non-JSON). A LOCAL endpoint that is
unavailable lets U8's day-split chain degrade to the heuristic.
"""

from __future__ import annotations

import json
import logging
from typing import Callable
from urllib.parse import urlparse, urlunparse

from screencap.segmentation.local_finish import build_local_prompt, finalize_local_result
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable

log = logging.getLogger(__name__)

# Cap the HTTP response body a (possibly hostile) local server can return, so it
# can't drive daemon memory pressure — analogous to the U2 stdout envelope bound.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024

_REQUEST_TIMEOUT_S = 120.0


def _pin_localhost(endpoint: str) -> str:
    """Rewrite a ``localhost`` host to ``127.0.0.1`` so /etc/hosts can't redirect it."""
    parsed = urlparse(endpoint)
    if (parsed.hostname or "").lower() == "localhost":
        netloc = parsed.netloc.replace("localhost", "127.0.0.1", 1)
        return urlunparse(parsed._replace(netloc=netloc))
    return endpoint


class LocalServerProvider:
    """Bring-your-own OpenAI-compatible / Ollama backend. See module docs.

    ``endpoint`` and ``raw_call`` are injectable for tests; production resolves the
    endpoint from config and posts via the default ``requests`` call.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        raw_call: Callable[[str, str], dict | None] | None = None,
        raw_answer: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._raw_call = raw_call if raw_call is not None else self._default_raw_call
        self._raw_answer = (
            raw_answer if raw_answer is not None else self._default_raw_answer
        )

    def segment(self, activity_summary: dict) -> dict | None | ProviderUnavailable:
        # Fail-closed privacy gate (R10): refuse unmarked input.
        if activity_summary.get("stripped") is not True:
            log.warning("LocalServerProvider refused an unmarked summary; unavailable")
            return PROVIDER_UNAVAILABLE

        endpoint = self._endpoint
        if endpoint is None:
            from screencap import config

            endpoint = config.get_local_server_endpoint()
        if not endpoint:
            log.info("No local-server endpoint configured; provider unavailable")
            return PROVIDER_UNAVAILABLE

        # Re-assert LOCAL on the exact string about to be POSTed (KTD8). Routing
        # classifies at build time, but the endpoint is re-read from config here,
        # so classify the value we will actually egress — never send off-box.
        from screencap.segmentation.endpoint import LOCAL, classify_endpoint

        if classify_endpoint(endpoint) != LOCAL:
            log.warning("Local-server endpoint is not LOCAL at send time; unavailable")
            return PROVIDER_UNAVAILABLE

        try:
            prompt = build_local_prompt(activity_summary["summary"])
        except Exception:  # pragma: no cover - defensive
            return PROVIDER_UNAVAILABLE

        raw = self._raw_call(_pin_localhost(endpoint), prompt)
        if raw is None:
            # Couldn't run (unreachable / timeout / non-JSON) → unavailable, so a
            # LOCAL endpoint lets the day-split chain degrade to the heuristic.
            return PROVIDER_UNAVAILABLE

        # Validate → sanitize (KTD12) → confidence-gate (KTD9), shared with the
        # downloaded backend (both surface model-generated names to the same sink).
        return finalize_local_result(raw, activity_summary)

    def answer(self, prompt: str, evidence: dict) -> str | ProviderUnavailable:
        """Recall-answer via the BYO local model server (SCR-243 path).

        Net-new generation seam. Mirrors :meth:`segment`'s posture — the same
        fail-closed ``stripped`` gate, the same endpoint resolution and the
        **connect-time LOCAL re-assertion** (KTD8: never egress off-box) — then
        posts the guardrail ``prompt`` for free-prose completion (no
        ``json_object`` response format; a recall answer is prose, not tasks).
        Returns the model's text on success, or
        :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` when the
        backend could not run. Never raises for an ordinary failure.
        """
        if evidence.get("stripped") is not True:
            log.warning("LocalServerProvider.answer refused an unmarked bundle; unavailable")
            return PROVIDER_UNAVAILABLE

        endpoint = self._endpoint
        if endpoint is None:
            from screencap import config

            endpoint = config.get_local_server_endpoint()
        if not endpoint:
            log.info("No local-server endpoint configured; answer unavailable")
            return PROVIDER_UNAVAILABLE

        # Re-assert LOCAL on the exact string about to be POSTed (KTD8) — same
        # send-time guard segment uses; never egress an answer off-box.
        from screencap.segmentation.endpoint import LOCAL, classify_endpoint

        if classify_endpoint(endpoint) != LOCAL:
            log.warning("Local-server endpoint is not LOCAL at send time; answer unavailable")
            return PROVIDER_UNAVAILABLE

        try:
            text = self._raw_answer(_pin_localhost(endpoint), prompt)
        except Exception:
            log.warning("Local-server answer call raised; unavailable", exc_info=True)
            return PROVIDER_UNAVAILABLE
        if text is None:
            return PROVIDER_UNAVAILABLE
        return text

    @staticmethod
    def _default_raw_call(endpoint: str, prompt: str) -> dict | None:
        """Live OpenAI-compatible call. Not run in CI (needs a server + requests)."""
        try:
            import requests
        except ImportError:  # pragma: no cover
            return None
        # Accept the endpoint with or without a trailing ``/v1`` — LM Studio and
        # Ollama present their OpenAI-compatible base URL as ``.../v1``, so naive
        # concatenation would double it to ``/v1/v1/chat/completions`` (404).
        base = endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")].rstrip("/")
        url = base + "/v1/chat/completions"
        body = {
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "stream": False,
        }
        try:
            # ``stream=True`` so the size cap bounds what we read into memory — a
            # non-streaming read fully buffers the body before any cap can fire,
            # letting a hostile local server drive daemon memory pressure (KTD8).
            with requests.post(
                url,
                json=body,
                timeout=_REQUEST_TIMEOUT_S,
                allow_redirects=False,  # a 302 must not bounce us off-box (KTD8)
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        log.warning("Local-server response exceeds the size cap; unavailable")
                        return None
                    chunks.append(chunk)
            content = json.loads(b"".join(chunks))["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            log.warning("Local-server call failed", exc_info=True)
            return None

    @staticmethod
    def _default_raw_answer(endpoint: str, prompt: str) -> str | None:
        """Live OpenAI-compatible completion for recall-answering. Text or None.

        Mirrors :meth:`_default_raw_call` (same KTD8 hardening: no redirects,
        capped streamed body, pinned host) but WITHOUT the ``json_object``
        response format — the completion is free prose, and the inner content is
        returned verbatim rather than parsed as task JSON. Not run in CI (needs a
        server + ``requests``).
        """
        try:
            import requests
        except ImportError:  # pragma: no cover
            return None
        base = endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")].rstrip("/")
        url = base + "/v1/chat/completions"
        body = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": False,
        }
        try:
            with requests.post(
                url,
                json=body,
                timeout=_REQUEST_TIMEOUT_S,
                allow_redirects=False,  # a 302 must not bounce us off-box (KTD8)
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        log.warning("Local-server answer exceeds the size cap; unavailable")
                        return None
                    chunks.append(chunk)
            content = json.loads(b"".join(chunks))["choices"][0]["message"]["content"]
            return content if isinstance(content, str) else None
        except Exception:
            log.warning("Local-server answer call failed", exc_info=True)
            return None
