"""Per-task cloud-consent policy — the single enforcement point (U6).

This module encodes the per-task consent matrix (R6–R10) and answers one
question for every Intelligence task: *given the task kind and whether an
on-device model is available right now, which execution target is this task
allowed to use?*

It is deliberately the **only** place the matrix lives. The degradation
resolver (U7) and the ``settings intelligence`` CLI (U8) route through
:class:`ConsentPolicy` — they do not re-decide the rules. Concentrating the
decision here means the fixed privacy guards (R7/R9/R10) cannot be inverted by
a change elsewhere.

Resolved product decision (authoritative)
-----------------------------------------
Summary/title **prefers on-device**. The cloud provider is used only as a
**fallback when on-device is unavailable AND the summary consent row is
enabled** — the consent row grants fallback permission, not always-cloud. The
same preference order applies to recall-answering (R10).

Fixed guards (checked before the cloud row)
-------------------------------------------
- **Day-split / label** is on-device-only regardless of cloud config (R7).
  When on-device is unavailable it degrades to the local idle-gap heuristic,
  **never** cloud (KTD6). It is the caller's (U7) job to run that heuristic;
  the policy only names the target.
- **Frames / images** are never sent to any cloud provider — a fixed rule, not
  a toggle (R9). They resolve to :attr:`ExecutionTarget.NEVER` for every
  configuration.

Only summary/title and recall-answer can ever resolve to cloud, and only as
the consented fallback described above.

This module is import-light and cloud-free: it imports nothing from any vendor
SDK and does not import :mod:`screencap.config` at module load — config is read
lazily inside :meth:`ConsentPolicy.from_config`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TaskKind(enum.Enum):
    """The Intelligence task kinds governed by the consent matrix.

    ``DAY_SPLIT`` covers both day-splitting and recording labeling (they share
    the on-device-only rule, R7). ``FRAMES`` covers screen frames / images
    (never-cloud, R9).
    """

    DAY_SPLIT = "day_split"
    SUMMARY = "summary"
    RECALL_ANSWER = "recall_answer"
    FRAMES = "frames"


class ExecutionTarget(enum.Enum):
    """Where a task is allowed to run, as resolved by :class:`ConsentPolicy`.

    - ``ON_DEVICE`` — run the on-device model.
    - ``CLOUD`` — run the configured cloud provider (transcript text only).
    - ``HEURISTIC`` — on-device unavailable and cloud is not permitted; fall
      back to the local idle-gap heuristic (day-split/label only, KTD6).
    - ``NEVER`` — cloud is a fixed-off rule for this kind (frames/images, R9).
    - ``NONE`` — no execution target is available (e.g. on-device unavailable,
      no consented cloud fallback, and no heuristic applies); the caller leaves
      the task unrun rather than failing or forcing an upload.
    """

    ON_DEVICE = "on_device"
    CLOUD = "cloud"
    HEURISTIC = "heuristic"
    NEVER = "never"
    NONE = "none"


@dataclass(frozen=True)
class ConsentPolicy:
    """Immutable snapshot of the consent configuration + resolution logic.

    Build one with :meth:`from_config` (reads the config getters) or construct
    it directly in tests. Then call :meth:`resolve` per task.

    Attributes
    ----------
    cloud_provider:
        The configured cloud provider name, or ``None`` when no cloud backend
        is configured. A task can only resolve to :attr:`ExecutionTarget.CLOUD`
        when this is set.
    summary_cloud_consent:
        Whether the summary/title cloud-consent row is enabled (R8).
    recall_cloud_consent:
        Whether the recall-answer cloud-consent row is enabled (R10).
    """

    cloud_provider: str | None = None
    summary_cloud_consent: bool = False
    recall_cloud_consent: bool = False

    @classmethod
    def from_config(cls) -> ConsentPolicy:
        """Build a policy from the config getters (env > toml > default).

        Imported lazily so this module stays free of a hard :mod:`screencap.config`
        dependency at import time.
        """
        from screencap import config

        return cls(
            cloud_provider=config.get_llm_cloud_provider(),
            summary_cloud_consent=config.get_summary_cloud_consent(),
            recall_cloud_consent=config.get_recall_cloud_consent(),
        )

    def resolve(
        self, task_kind: TaskKind, *, on_device_available: bool
    ) -> ExecutionTarget:
        """Return the allowed execution target for ``task_kind``.

        The fixed guards are checked **first**, before any cloud row is
        consulted, so R7/R9/R10 cannot be inverted by the consent config:

        - ``FRAMES`` → :attr:`~ExecutionTarget.NEVER`, unconditionally (R9).
        - ``DAY_SPLIT`` → :attr:`~ExecutionTarget.ON_DEVICE` when available,
          else :attr:`~ExecutionTarget.HEURISTIC` — cloud is never reachable
          (R7 / KTD6).

        Only ``SUMMARY`` and ``RECALL_ANSWER`` can reach cloud, and only as the
        consented fallback: on-device is preferred whenever available; cloud is
        used only when on-device is unavailable **AND** the task's consent row
        is on **AND** a cloud provider is configured. Otherwise the task
        resolves to :attr:`~ExecutionTarget.ON_DEVICE` (when available) or
        :attr:`~ExecutionTarget.NONE`.
        """
        # --- Fixed guards (never consult the cloud row) ---------------------
        if task_kind is TaskKind.FRAMES:
            return ExecutionTarget.NEVER  # R9 — fixed rule, not a toggle.

        if task_kind is TaskKind.DAY_SPLIT:
            # R7 / KTD6 — on-device only; degrade to the heuristic, never cloud.
            return (
                ExecutionTarget.ON_DEVICE
                if on_device_available
                else ExecutionTarget.HEURISTIC
            )

        # --- Cloud-eligible kinds: prefer on-device, cloud is the fallback --
        if task_kind is TaskKind.SUMMARY:
            consented = self.summary_cloud_consent
        elif task_kind is TaskKind.RECALL_ANSWER:
            consented = self.recall_cloud_consent
        else:  # defensive: unknown kind never reaches cloud.
            return (
                ExecutionTarget.ON_DEVICE
                if on_device_available
                else ExecutionTarget.NONE
            )

        if on_device_available:
            return ExecutionTarget.ON_DEVICE  # Prefer on-device (decision).
        if consented and self.cloud_provider is not None:
            return ExecutionTarget.CLOUD  # Consented fallback only.
        return ExecutionTarget.NONE
