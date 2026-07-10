"""Graceful-degradation resolver — what to do when a provider can't run (U7, R5).

The segmentation provider's :meth:`~screencap.segmentation.provider.LLMProvider.segment`
returns one of three shapes (see ``provider.py``): a validated **tasks dict**
(ran, produced tasks), ``None`` (ran, no usable tasks), or
:data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` (could not run at
all). This module maps that outcome — combined with the per-task
:class:`~screencap.segmentation.consent.ConsentPolicy` — onto a single
:class:`Degradation` decision, so the terminal day-split path has ONE place that
decides "provider unavailable → idle-gap heuristic".

The one rule that must not drift (KTD6, R7 over R5)
---------------------------------------------------
The **day-split / label** task degrades on-device → the local idle-gap heuristic
ONLY. Cloud is **never** a day-split fallback, even when a cloud provider is
configured and consented — R7 wins over R5. :class:`ConsentPolicy` already
encodes this (``DAY_SPLIT`` can only resolve to ``ON_DEVICE`` or ``HEURISTIC``,
never ``CLOUD``); :func:`resolve_day_split` re-asserts it defensively so the
never-cloud rule has a single verifiable home here too.

``None`` vs ``PROVIDER_UNAVAILABLE`` (the distinction U7 is built on)
--------------------------------------------------------------------
Only :data:`PROVIDER_UNAVAILABLE` triggers the heuristic. A genuine ``None``
(the provider RAN and produced nothing) is left as a fail-open no-tasks result —
we do NOT invent an idle-gap split for a recording the model deliberately
declined to name. The heuristic is specifically the on-device-*unavailable*
fallback, not an empty-result backstop.

This module is import-light and cloud-free: it imports only the sibling
consent/provider modules (both themselves cloud-free) at load time.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from screencap.segmentation.consent import (
    ConsentPolicy,
    ExecutionTarget,
    TaskKind,
)
from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    AnswerResult,
    SegmentResult,
)


class DegradeAction(enum.Enum):
    """What the caller should do with a task after consulting the ladder.

    - ``USE_PROVIDER`` — the provider returned real tasks; persist them
      unchanged. ``tasks`` on the :class:`Degradation` carries the dict.
    - ``HEURISTIC`` — the provider could not run; fall back to the local
      idle-gap heuristic (day-split/label only, KTD6). The caller builds the
      task boundaries and persists them.
    - ``CLOUD`` — the provider could not run and this (consented) task may use
      the configured cloud provider. Reachable only for SUMMARY/RECALL_ANSWER,
      **never** for DAY_SPLIT (R7 over R5). Not exercised by terminal_stage's
      day-split path today; it is the resolver's capability for the future
      on-demand summary/title flow.
    - ``NONE`` — nothing to run: the provider ran and produced nothing
      (fail-open no-tasks), or no execution target is available. The caller
      leaves the recording unnamed; it NEVER hard-fails and NEVER uploads.
    """

    USE_PROVIDER = "use_provider"
    HEURISTIC = "heuristic"
    CLOUD = "cloud"
    NONE = "none"


@dataclass(frozen=True)
class Degradation:
    """The resolved decision for one task.

    ``tasks`` is set only for a segmentation ``USE_PROVIDER`` (a validated tasks
    dict); ``answer`` is set only for a recall-answer ``USE_PROVIDER`` (the
    generated prose). They are the two provider-result payloads the two resolve
    entry points carry; both are ``None`` for HEURISTIC/CLOUD/NONE.
    """

    action: DegradeAction
    tasks: dict | None = None
    answer: str | None = None


def resolve(
    task_kind: TaskKind,
    provider_result: SegmentResult,
    policy: ConsentPolicy,
) -> Degradation:
    """Map ``(task_kind, provider_result, policy)`` onto a :class:`Degradation`.

    Decision order:

    1. A real tasks dict → :attr:`DegradeAction.USE_PROVIDER` (the provider ran
       and produced tasks; use them unchanged, regardless of kind or consent).
    2. ``None`` → :attr:`DegradeAction.NONE` (ran, no usable tasks — fail-open;
       the heuristic is NOT a substitute for a genuine empty result).
    3. :data:`PROVIDER_UNAVAILABLE` → route through :class:`ConsentPolicy` with
       ``on_device_available=False``:

       - ``DAY_SPLIT`` resolves to ``HEURISTIC`` (never cloud — R7/KTD6). This is
         re-asserted below rather than trusted implicitly.
       - a consented ``SUMMARY`` / ``RECALL_ANSWER`` resolves to ``CLOUD``.
       - otherwise ``NONE``.
    """
    # 1. Provider ran and produced tasks (a truthy, non-sentinel dict).
    if isinstance(provider_result, dict):
        return Degradation(DegradeAction.USE_PROVIDER, tasks=provider_result)

    # 2. Provider ran and produced nothing usable — fail-open, no heuristic.
    #    (PROVIDER_UNAVAILABLE is falsy too, so check identity FIRST.)
    if provider_result is not PROVIDER_UNAVAILABLE:
        return Degradation(DegradeAction.NONE)

    # 3. Provider could not run — consult the consent matrix (on-device off).
    target = policy.resolve(task_kind, on_device_available=False)
    if target is ExecutionTarget.HEURISTIC:
        return Degradation(DegradeAction.HEURISTIC)
    if target is ExecutionTarget.CLOUD:
        return Degradation(DegradeAction.CLOUD)
    # ON_DEVICE is unreachable here (on_device_available=False); NEVER/NONE and
    # any unexpected target all mean "leave it unrun".
    return Degradation(DegradeAction.NONE)


def resolve_day_split(
    provider_result: SegmentResult,
    policy: ConsentPolicy,
) -> Degradation:
    """Day-split-specific resolve with the never-cloud rule pinned here (KTD6).

    Delegates to :func:`resolve` for :attr:`TaskKind.DAY_SPLIT`, then **asserts**
    the result is never :attr:`DegradeAction.CLOUD`. :class:`ConsentPolicy`
    already guarantees a day-split can only resolve to on-device/heuristic, so
    this is a defensive guard, not new policy — it gives the "cloud is never a
    day-split fallback" invariant (R7 over R5) a single, test-visible home in the
    degradation layer, independent of the consent module.
    """
    decision = resolve(TaskKind.DAY_SPLIT, provider_result, policy)
    if decision.action is DegradeAction.CLOUD:  # pragma: no cover - guard
        raise AssertionError(
            "day-split must never degrade to cloud (R7 over R5 / KTD6); "
            "ConsentPolicy.resolve returned CLOUD for DAY_SPLIT"
        )
    return decision


def resolve_answer(
    answer_result: AnswerResult,
    policy: ConsentPolicy,
) -> Degradation:
    """Map a recall-answer ``(str | PROVIDER_UNAVAILABLE)`` onto a :class:`Degradation`.

    The generation seam (U1) returns a two-shape :data:`AnswerResult` — prose
    (:class:`str`) or :data:`PROVIDER_UNAVAILABLE` — NOT the tri-state
    :data:`SegmentResult` :func:`resolve` is typed to (there is no ``None``
    "ran, no usable output" shape: an empty/declined prose answer is still a
    ``str``, and grounding is enforced behaviorally downstream, not here). So it
    can't reuse :func:`resolve` as-is; it reuses the **routing logic** —
    :data:`PROVIDER_UNAVAILABLE` + :class:`ConsentPolicy` on
    :attr:`TaskKind.RECALL_ANSWER` — over the answer shape:

    - a prose ``str`` → :attr:`DegradeAction.USE_PROVIDER` (the provider ran and
      answered; ``answer`` on the :class:`Degradation` carries the text).
    - :data:`PROVIDER_UNAVAILABLE` → route through :class:`ConsentPolicy` with
      ``on_device_available=False`` for ``RECALL_ANSWER``: a consented cloud
      fallback resolves to :attr:`DegradeAction.CLOUD`; otherwise
      :attr:`DegradeAction.NONE`. It never raises for an unavailable backend —
      the ladder degrades to a consented cloud provider or leaves the turn
      unanswered.
    """
    # Provider ran and produced prose (str) — use it, regardless of consent.
    if isinstance(answer_result, str):
        return Degradation(DegradeAction.USE_PROVIDER, answer=answer_result)

    # Anything that is not a str is the could-not-run sentinel — consult the
    # consent matrix (on-device off) on the RECALL_ANSWER kind.
    target = policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)
    if target is ExecutionTarget.CLOUD:
        return Degradation(DegradeAction.CLOUD)
    # RECALL_ANSWER never resolves to HEURISTIC (that is the day-split fallback);
    # ON_DEVICE is unreachable with on_device_available=False. NONE otherwise.
    return Degradation(DegradeAction.NONE)
