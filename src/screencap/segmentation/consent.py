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
**fallback when on-device is unavailable AND a cloud provider is configured** —
connecting a provider is the consent (KTD1), so the per-task consent rows default
**on** and the Intelligence pane no longer shows a toggle. The rows survive as a
CLI/env override to disable cloud fallback. The consent still grants only
fallback permission, not always-cloud. The same preference order applies to
recall-answering (R10). This module's ``resolve`` logic is unchanged by the
default flip: it already gates ``CLOUD`` on ``cloud_provider is not None``.

Fixed guards (checked before the cloud row)
-------------------------------------------
- **Day-split / label** is on-device-only regardless of cloud config (R7).
  When on-device is unavailable it degrades to the local idle-gap heuristic,
  **never** cloud (KTD6). It is the caller's (U7) job to run that heuristic;
  the policy only names the target.
- **Frames / images** are never a standalone cloud task — a fixed rule, not a
  toggle (R9). :meth:`ConsentPolicy.resolve` returns
  :attr:`ExecutionTarget.NEVER` for ``TaskKind.FRAMES`` in every configuration.
  Frames may only ever *ride along* on a task that has already resolved to
  cloud, and only with the independent, default-off ``frames_cloud_consent``
  opt-in — that layered decision lives in :func:`frames_may_attach`, never in
  :meth:`resolve` (SCR-272).

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
        Whether summaries/titles may use the cloud fallback (R8). Defaults on in
        config (KTD1); the field default here stays ``False`` because this is the
        explicit value object — the product default is applied in
        :meth:`from_config` via ``config.get_summary_cloud_consent``.
    recall_cloud_consent:
        Whether recall-answers may use the cloud fallback (R10). Same default
        split as ``summary_cloud_consent``.
    frames_cloud_consent:
        Whether masked screen frames may be *attached* to an already-cloud
        summary/recall task (SCR-272). An independent, default-**off** opt-in:
        it is consulted only by :func:`frames_may_attach`, layered on top of a
        task that has already resolved to :attr:`ExecutionTarget.CLOUD`. It
        **never** changes :meth:`resolve` — ``TaskKind.FRAMES`` stays
        :attr:`ExecutionTarget.NEVER` for every configuration (R9).
    """

    cloud_provider: str | None = None
    # Field defaults stay False (explicit "unset" value object); the shipped
    # product default (on) is applied by `from_config` via the config getters.
    summary_cloud_consent: bool = False
    recall_cloud_consent: bool = False
    # Frames stay off by default in config too (SCR-272) — independent opt-in.
    frames_cloud_consent: bool = False

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
            frames_cloud_consent=config.get_frames_cloud_consent(),
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


def frames_may_attach(
    policy: ConsentPolicy, resolved_target: ExecutionTarget
) -> bool:
    """Return whether masked frames may ride along on an already-cloud task (SCR-272).

    This is the frame-egress gate, deliberately **separate** from
    :meth:`ConsentPolicy.resolve`: frames are never a standalone cloud task
    (``resolve(TaskKind.FRAMES)`` is always :attr:`ExecutionTarget.NEVER`, R9).
    Instead, a summary/recall task that has *already* resolved to
    :attr:`ExecutionTarget.CLOUD` may — only with explicit opt-in — carry masked
    frames as multimodal evidence.

    Returns ``True`` only when **all** hold:

    - ``resolved_target is ExecutionTarget.CLOUD`` — the host task is actually
      cloud-bound (an ``ON_DEVICE`` / ``NEVER`` / ``NONE`` task never attaches
      frames);
    - ``policy.frames_cloud_consent`` — the independent, default-off frames
      opt-in is set (connecting a provider + cloud-tasks-on does **not** imply
      it, AE1);
    - ``policy.cloud_provider is not None`` — there is a configured backend to
      send them to.
    """
    return (
        resolved_target is ExecutionTarget.CLOUD
        and policy.frames_cloud_consent
        and policy.cloud_provider is not None
    )
