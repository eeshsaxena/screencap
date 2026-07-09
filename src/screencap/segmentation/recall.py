"""Recall-answer dispatcher (SCR-243, U8).

:func:`answer_recall` is the single entry point Conversational Recall Chat
calls. It enforces the fail-closed stripped gate (R11), builds the on-device-class
answer chain via :func:`~screencap.segmentation.routing.build_answer_provider`,
and — only when the whole on-device chain is unavailable — falls back to the
configured cloud provider under the ``RECALL_ANSWER`` consent row (R9/R10). The
cloud target is always the configured cloud provider, never a BYO endpoint.

Two-state return: ``str | PROVIDER_UNAVAILABLE`` (KTD9). Never raises for an
ordinary model/API error — including a future cloud provider name that does not
implement the generation seam.

This module is import-light and cloud-free at load: config, routing, consent, and
the provider factory are imported lazily inside the call.
"""

from __future__ import annotations

import logging

from screencap.segmentation.generation import Evidence, GenerationProvider
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable

log = logging.getLogger(__name__)

# Bound the request once, before either the on-device or cloud path runs — the
# single home for the DoS/OOM/egress-size cap (mirrors the per-backend caps).
_MAX_EVIDENCE_BYTES = 512 * 1024
_MAX_PROMPT_BYTES = 16 * 1024


def answer_recall(prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
    """Answer ``prompt`` grounded in stripped ``evidence``: on-device first, then
    consented cloud. See the module docstring for the contract.
    """
    # Fail-closed privacy gate (R11): refuse unmarked evidence, build nothing.
    if getattr(evidence, "stripped", False) is not True:
        log.warning("answer_recall refused evidence not marked stripped=True; unavailable")
        return PROVIDER_UNAVAILABLE

    # Bound the request before any spawn / cloud egress.
    if (
        len(evidence.text.encode("utf-8")) > _MAX_EVIDENCE_BYTES
        or len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES
    ):
        log.warning("answer_recall request exceeds the size cap; unavailable")
        return PROVIDER_UNAVAILABLE

    from screencap.segmentation.routing import build_answer_provider

    on_device = build_answer_provider().answer(prompt, evidence)
    if on_device is not PROVIDER_UNAVAILABLE:
        return on_device  # a grounded str answer stops here

    # The whole on-device chain is unavailable — consult the consent matrix.
    return _cloud_fallback(prompt, evidence)


def _cloud_fallback(prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
    from screencap import config
    from screencap.segmentation.consent import ConsentPolicy, ExecutionTarget, TaskKind

    target = ConsentPolicy.from_config().resolve(
        TaskKind.RECALL_ANSWER, on_device_available=False
    )
    if target is not ExecutionTarget.CLOUD:
        return PROVIDER_UNAVAILABLE

    cloud_name = config.get_llm_cloud_provider()
    if not cloud_name:
        return PROVIDER_UNAVAILABLE

    from screencap.segmentation.provider import get_provider

    try:
        provider = get_provider(cloud_name)
    except ValueError:
        log.warning("Configured cloud provider %r is unknown; unavailable", cloud_name)
        return PROVIDER_UNAVAILABLE

    # A future cloud provider name that does not implement the generation seam
    # must degrade, not raise (R8).
    if not isinstance(provider, GenerationProvider):
        log.warning("Cloud provider %r does not implement answer(); unavailable", cloud_name)
        return PROVIDER_UNAVAILABLE

    result = provider.answer(prompt, evidence)
    return result if isinstance(result, str) else PROVIDER_UNAVAILABLE
