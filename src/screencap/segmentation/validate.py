"""Validate + normalize LLM segmentation output.

Lifted verbatim from the Cloud Run processor (``scripts/process-recording/main.py``):
converts the LLM's relative timestamps to Unix, clamps to session bounds,
rejects overlapping / zero-duration tasks, and repairs recoverable schema
drift (missing summary fields, bad categories, tag normalization).

Cloud-free — no ``google.cloud`` / ``genai`` imports — so it stays importable
inside the daemon.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_VALID_CATEGORIES = frozenset(
    {"development", "communication", "research", "admin", "creative", "other"}
)

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_MAX_TAGS = 8


def _slugify(text: str, max_len: int = 40) -> str:
    s = text.lower()
    # Strip PII placeholder tags like <PERSON>, <EMAIL>, etc.
    s = re.sub(r"<[A-Z_]+>", "", s)
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return (s or "untitled")[:max_len]


def _parse_relative_time(rel: str) -> float:
    """Parse H:MM:SS relative timestamp back to seconds.

    Returns 0.0 on malformed input instead of crashing.
    """
    try:
        parts = rel.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(parts[0])
    except (ValueError, TypeError):
        log.warning("Malformed relative timestamp: %r", rel)
        return 0.0


def _validate_tags(raw: list) -> list[str]:
    """Validate and normalize LLM-generated tags."""
    seen: set[str] = set()
    result: list[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.lower().strip()
        if _TAG_RE.match(t) and t not in seen:
            seen.add(t)
            result.append(t)
        if len(result) >= _MAX_TAGS:
            break
    return result


def validate_llm_tasks(
    llm_result: dict,
    session_start: float,
    session_end: float,
    time_map: dict[str, float],
) -> dict | None:
    """Validate LLM output and convert relative timestamps to Unix.

    Returns dict with "tasks" and "summary", or None if invalid.
    """
    tasks = llm_result.get("tasks", [])
    summary = llm_result.get("summary", {})

    if not tasks:
        log.warning("LLM returned empty tasks list")
        return None

    converted: list[dict] = []

    for i, task in enumerate(tasks):
        for field in ("start_time", "end_time", "name", "description", "category"):
            if field not in task:
                log.warning("Task %d missing field: %s", i, field)
                return None

        start_rel = task["start_time"]
        end_rel = task["end_time"]

        start_unix = time_map.get(start_rel, session_start + _parse_relative_time(start_rel))
        end_unix = time_map.get(end_rel, session_start + _parse_relative_time(end_rel))

        # Clamp to session bounds
        start_unix = max(session_start, min(start_unix, session_end))
        end_unix = max(session_start, min(end_unix, session_end))

        if end_unix <= start_unix:
            log.warning("Task %d has zero or negative duration", i)
            return None

        cat = task.get("category", "other")
        if cat not in _VALID_CATEGORIES:
            cat = "other"

        name = (task.get("name") or f"Task {i + 1}").strip()[:80]

        converted.append({
            "start_ts": start_unix,
            "end_ts": end_unix,
            "name": name,
            "derived_name": _slugify(name),
            "description": (task.get("description") or "")[:600],
            "category": cat,
            "apps_used": task.get("apps_used", []),
            "confidence": task.get("confidence", "medium"),
            # Compatibility defaults for _process_task
            "dominant_app": "",
            "dominant_app_name": "",
            "dominant_title": "",
            "dominant_pct": 0.0,
            "all_apps": {},
            "rest_after_s": 0.0,
            "event_count": 0,
        })

    converted.sort(key=lambda t: t["start_ts"])

    # Check for overlaps (1s tolerance)
    for i in range(len(converted) - 1):
        if converted[i]["end_ts"] > converted[i + 1]["start_ts"] + 1.0:
            log.warning("Tasks %d and %d overlap", i, i + 1)
            return None

    # Compute rest_after_s
    for i in range(len(converted) - 1):
        gap = converted[i + 1]["start_ts"] - converted[i]["end_ts"]
        converted[i]["rest_after_s"] = round(max(0, gap), 1)
    if converted:
        converted[-1]["rest_after_s"] = 0.0

    # Ensure summary has all required fields
    if not summary.get("overview"):
        summary["overview"] = f"Recording with {len(converted)} tasks."
    if not summary.get("primary_focus"):
        cats = [t["category"] for t in converted]
        summary["primary_focus"] = max(set(cats), key=cats.count) if cats else "other"
    if not summary.get("time_breakdown"):
        summary["time_breakdown"] = {}
    if not summary.get("key_accomplishments"):
        summary["key_accomplishments"] = []

    tags = _validate_tags(llm_result.get("tags", []))

    return {"tasks": converted, "summary": summary, "tags": tags}
