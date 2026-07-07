"""Confidence gate — "never mislead" for the local-model backends (U3, R9/KTD9).

A shared post-validate step: for each task whose self-reported ``confidence`` is
at or below the configured threshold — **or missing/unparseable** — keep the task
boundary but blank the name so it renders *unnamed* rather than showing a name the
model wasn't confident in. Schema-invalid whole results are already rejected
upstream by ``validate_llm_tasks``; this gate handles the per-task case (F3).

Fail-closed on a missing enum (KTD9)
------------------------------------
``validate_llm_tasks`` defaults an absent ``confidence`` to ``"medium"`` — which is
*above* the ``low`` gate, so an omitted field would otherwise slip through as a
named task. A 3B model on the MLX path (no schema-constrained decoding) omitting a
required field is a common failure mode, so a missing/unparseable confidence is
treated as **below every threshold** here and always blanked.

Applied by the local-model backends (the downloaded model and the BYO local
server), the two weakest links. The threshold is config-driven and calibrated by
the U12 eval.
"""

from __future__ import annotations

# Confidence levels, lowest to highest. A missing/unknown value ranks below all
# of them (-1), so it is blanked at any threshold (fail-closed).
_RANK = {"low": 0, "medium": 1, "high": 2}

#: Default threshold — blank names at or below ``low`` (plus missing).
DEFAULT_THRESHOLD = "low"


def _rank(value: object) -> int:
    return _RANK.get(value, -1) if isinstance(value, str) else -1


def apply_confidence_gate(
    tasks_dict: dict | None, threshold: str = DEFAULT_THRESHOLD
) -> dict | None:
    """Blank the name of every task at/below ``threshold`` confidence (or missing).

    ``None`` passes through unchanged. A task is *blanked* (``name``/``derived_name``
    **and** the model-written ``description`` set to ``""`` — an unnamed boundary the
    UI renders as such) when its confidence rank is ``<=`` the threshold rank; a
    missing/unparseable confidence ranks below all thresholds and is always blanked.
    The task itself is kept (boundary preserved), never dropped. An unknown
    ``threshold`` falls back to :data:`DEFAULT_THRESHOLD`.

    The ``description`` is blanked with the name because it is the same
    low-confidence model guess reaching the same sink — leaving it populated would
    surface free text the model wasn't confident in, defeating "never mislead".
    """
    if tasks_dict is None:
        return None
    thr = _RANK.get(threshold, _RANK[DEFAULT_THRESHOLD])
    tasks = tasks_dict.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if _rank(task.get("confidence")) <= thr:
                task["name"] = ""
                task["derived_name"] = ""
                task["description"] = ""
    return tasks_dict
