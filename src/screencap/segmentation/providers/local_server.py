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
    ) -> None:
        self._endpoint = endpoint
        self._raw_call = raw_call if raw_call is not None else self._default_raw_call

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

    @staticmethod
    def _default_raw_call(endpoint: str, prompt: str) -> dict | None:
        """Live OpenAI-compatible call. Not run in CI (needs a server + requests)."""
        try:
            import requests
        except ImportError:  # pragma: no cover
            return None
        url = endpoint.rstrip("/") + "/v1/chat/completions"
        body = {
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "stream": False,
        }
        try:
            resp = requests.post(
                url,
                json=body,
                timeout=_REQUEST_TIMEOUT_S,
                allow_redirects=False,  # a 302 must not bounce us off-box (KTD8)
            )
            if resp.status_code != 200:
                return None
            if len(resp.content) > _MAX_RESPONSE_BYTES:
                log.warning("Local-server response exceeds the size cap; unavailable")
                return None
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            log.warning("Local-server call failed", exc_info=True)
            return None
