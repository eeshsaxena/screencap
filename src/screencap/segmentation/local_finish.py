"""Shared local-model finish: prompt build + validate→sanitize→gate (SCR-239).

The downloaded-model and BYO local-server backends share one post-processing tail —
they are "the two weakest links" the confidence gate (KTD9) and the untrusted-output
sanitizer (KTD12) exist to protect. Keeping that tail (and its load-bearing
sanitize-before-gate ordering) in one place means a change to it can't be applied to
only one backend.
"""

from __future__ import annotations

import json
import logging

from screencap.segmentation.confidence_gate import apply_confidence_gate
from screencap.segmentation.sanitize import sanitize_tasks
from screencap.segmentation.validate import validate_llm_tasks

log = logging.getLogger(__name__)


def build_local_prompt(summary: dict) -> str:
    """Format the canonical segmentation prompt from the (stripped) summary.

    Reuses the shared prompt so local and cloud drive one prompt; ``_LLM_PROMPT``
    is imported lazily so importing this module stays cloud-free.
    """
    from screencap.segmentation.providers.gemini import _LLM_PROMPT

    return _LLM_PROMPT.format(activity_json=json.dumps(summary, indent=2))


def finalize_local_result(raw: dict, activity_summary: dict) -> dict | None:
    """Validate → sanitize (KTD12) → confidence-gate (KTD9) a raw model result.

    Order is load-bearing: the sanitizer defaults an empty name to a placeholder,
    so the gate's blanking of low/absent-confidence names must run last. Returns
    the finished tasks dict, or ``None`` when the output doesn't validate.
    """
    try:
        validated = validate_llm_tasks(
            raw,
            activity_summary["session_start"],
            activity_summary["session_end"],
            activity_summary["time_map"],
        )
    except Exception:
        log.warning("Local-model output failed validation", exc_info=True)
        return None
    from screencap import config

    sanitized = sanitize_tasks(validated)
    return apply_confidence_gate(sanitized, config.get_confidence_gate_threshold())
