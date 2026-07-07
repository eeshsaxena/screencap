"""Untrusted-output sanitizer for model-generated task fields (KTD12, SCR-239).

Task names/descriptions produced by a local model derive from screen/transcript
content — attacker-influenceable — and are surfaced through ``/v0/tasks.list`` and
the MCP surface to downstream agents. A prompt injection can yield a *confident*
name carrying markup or control sequences aimed at the next consumer, which the
confidence gate (KTD9, low-confidence only) does not catch. So every provider that
surfaces model-generated names runs its **validated** output through
:func:`sanitize_tasks` before persistence: strip control characters and markup,
bound field lengths, and cap the task count/shape.

This is applied *after* ``validate_llm_tasks`` (which already normalizes
timestamps and rejects overlaps) — it hardens the free-text fields the validator
passes through, independent of the confidence gate.
"""

from __future__ import annotations

import re

# Bound the number of tasks a single model response can persist (a hostile /
# runaway model could otherwise emit a huge list → tasks-store / UI DoS).
_MAX_TASKS = 200

# Field length caps (mirror the validator's own caps; re-applied defensively
# after markup stripping, which can shift lengths).
_MAX_NAME_LEN = 80
_MAX_DESCRIPTION_LEN = 600

# Control characters (C0 minus tab/newline, plus DEL and C1) are never legitimate
# in a task name/description and are prime injection vectors.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Angle-bracket markup — strip whole ``<...>`` spans so an injected ``<script>``
# or a fake tag can't reach a Markdown/HTML consumer.
_MARKUP_RE = re.compile(r"<[^>]*>")

_WHITESPACE_RE = re.compile(r"\s+")


def _clean_text(value: object, max_len: int) -> str:
    """Return ``value`` as a control-char-free, markup-free, length-bounded string."""
    if not isinstance(value, str):
        return ""
    text = _MARKUP_RE.sub("", value)
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_len]


def sanitize_tasks(tasks_dict: dict | None) -> dict | None:
    """Sanitize the free-text fields of a validated tasks dict in place-safe form.

    ``None`` passes through unchanged (nothing to sanitize). Otherwise the task
    list is capped at :data:`_MAX_TASKS`, and each task's ``name`` / ``description``
    are stripped of control characters and markup and length-bounded. Other
    validated fields (timestamps, category enum, confidence) are left as the
    validator produced them.
    """
    if tasks_dict is None:
        return None

    tasks = tasks_dict.get("tasks")
    if isinstance(tasks, list):
        capped = tasks[:_MAX_TASKS]
        for task in capped:
            if not isinstance(task, dict):
                continue
            task["name"] = _clean_text(task.get("name"), _MAX_NAME_LEN) or "untitled"
            task["description"] = _clean_text(
                task.get("description"), _MAX_DESCRIPTION_LEN
            )
        tasks_dict["tasks"] = capped

    return tasks_dict
