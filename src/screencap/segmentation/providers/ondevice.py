"""On-device (Apple Foundation Models) backend for the provider interface.

Apple Foundation Models is Swift-only and macOS-26+, so the Python provider
cannot call it in-process (KTD2). This backend shells out to a small **Swift
helper** (``macos/IntelligenceHelper``, shipped inside the macOS app bundle):
it writes the activity summary as JSON to the helper's stdin, reads a status +
tasks JSON envelope back from stdout, and runs the tasks through the shared
:func:`~screencap.segmentation.validate.validate_llm_tasks` — the SAME repair/
reject net the cloud path uses, so weaker on-device structured output still
lands as usable tasks or is rejected cleanly.

Cloud-free & import-light: no SDK is imported at module load; only ``json`` /
``os`` / ``subprocess`` and the shared validator (itself cloud-free).

Return contract (see :mod:`screencap.segmentation.provider`)
------------------------------------------------------------
- a validated **tasks dict** — the helper ran the model and the output validated.
- ``None`` — the helper ran the model but the output was empty / did not
  validate and could not be repaired (a *ran, no usable tasks* outcome).
- :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` — the provider
  **could not run at all**: helper binary missing, OS below the macOS-26 floor,
  Apple Intelligence not enabled / model not ready, subprocess timed out,
  exited non-zero, or emitted an envelope that isn't parseable at all. U7's
  degradation ladder routes on this distinct sentinel.

Failure reasons (SCR-275, U3 / KTD-3)
-------------------------------------
The sentinel stays the routed-on identity, but WHY the provider was
unavailable now travels out-of-band: after every ``segment`` / ``answer`` /
``call_*`` invocation the provider records :attr:`OnDeviceProvider.
last_unavailable_reason` (``None`` on success). Reasons are either the
helper's own semantic strings (``bad-request``, ``model-unavailable-*``,
``context-window``, ``guardrail``, ``refusal``, ``rate-limited``,
``decoding-failure``, ``unsupported``, ``respond-failed`` — plus the legacy
``no-input`` / ``os-below-macos-26`` / ``foundationmodels-unavailable`` /
``result-encode-failed`` / ``answer-failed``) or this module's generic
``REASON_*`` strings for failures where no envelope ever arrived.

Window-scoped verbs (SCR-275, U3 / KTD-2)
-----------------------------------------
:meth:`OnDeviceProvider.call_arbitrate`, :meth:`~OnDeviceProvider.
call_name_window` and :meth:`~OnDeviceProvider.call_day_summary` are one-shot
helper spawns (fresh model session per call) returning a :class:`CallResult`
— a verb-typed value XOR a failure reason. They share the legacy spawn path
(helper discovery, scrubbed env, stdout cap) but default to a shorter
per-call timeout (60s vs the whole-day 120s; the same
``SCREENCAP_ONDEVICE_HELPER_TIMEOUT`` env var overrides both). The KTD-3
retry taxonomy is applied at this layer: one fresh-spawn retry for
``decoding-failure``, one post-backoff retry for ``rate-limited``, and no
retry ever for ``guardrail`` / ``refusal`` / ``unsupported`` /
``bad-request`` — nor for ``context-window``, whose recovery (digest
halving on name-window) is caller-owned (the U4 orchestrator).

Fail-closed privacy contract (R11)
----------------------------------
This backend accepts ONLY a privacy-stripped activity summary. The strip is the
single chokepoint in ``build_activity_summary`` (U3); the local segmentation
stage (U4) marks the dict it hands here with ``stripped=True`` after building it
with a ``blocked_source``. If that marker is absent, :meth:`segment` refuses and
returns :data:`PROVIDER_UNAVAILABLE` WITHOUT spawning the helper — an unmarked
summary must never reach the model. (Callers that have genuinely stripped the
input set the flag explicitly; there is no implicit trust.)

Helper discovery
----------------
1. ``SCREENCAP_ONDEVICE_HELPER`` env var (an explicit path) — used first. It is
   both a test seam (point it at a fake helper) and the PRIMARY production
   channel: the daemon launcher inside the app bundle exports it at
   ``…/Screencap.app/Contents/MacOS/IntelligenceHelper`` (the launcher knows the
   layout), covering both the nested-daemon bundle and dev-source runs.
2. The bundled helper, discovered by walking up to the enclosing
   ``Screencap.app/Contents/MacOS/IntelligenceHelper`` (see
   :func:`_find_bundled_helper`). This is the fallback when the env var is
   unset; it walks PAST the nested ``ScreencapDaemon.app`` Contents to the outer
   app. A source/dev checkout has no app-bundle ancestry, so this fallback finds
   nothing there — the launcher env in (1) is what makes dev/CI work.
   **CLI-only / headless installs have neither**, so discovery fails and the
   provider reports unavailable — exactly the case U7 degrades (heuristic for
   day-splitting; consented cloud for summaries).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from screencap.segmentation.generation import Evidence, MaskedFrame
from screencap.segmentation.generation_finish import evidence_gate_ok, sanitize_answer
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable
from screencap.segmentation.validate import validate_llm_tasks

log = logging.getLogger(__name__)

# The helper binary name inside the app bundle's MacOS dir.
_HELPER_BASENAME = "IntelligenceHelper"

# Wall-clock ceiling for one whole-day helper invocation (legacy ``segment`` /
# ``answer``). The on-device model is slow but bounded; a hang past this maps
# to unavailable so terminal_stage never blocks on segmentation. Overridable
# via env for the (fast) fake-helper tests.
_DEFAULT_TIMEOUT_S = 120.0

# Wall-clock ceiling for one window-scoped verb call (arbitrate / name-window /
# day-summary). Inputs are small and per-verb response caps are tight (KTD-2),
# so these calls get a much shorter default than the whole-day paths — strictly
# below any pass budget (KTD-7). The same env var overrides both.
_DEFAULT_VERB_TIMEOUT_S = 60.0

# Response size cap for the free-form answer path (KTD10 / security review) —
# also reused as the stdout cap for the (much smaller) window-scoped verbs.
# The request-side caps + fail-closed gate are shared via
# ``generation_finish.evidence_gate_ok``; this bounds the helper's stdout result.
_MAX_ANSWER_STDOUT_BYTES = 1 * 1024 * 1024

# ---------------------------------------------------------------------------
# Failure reasons (KTD-3). The helper reports SEMANTIC reasons inside its
# unavailable envelope; these GENERIC ones are recorded by this layer when no
# parseable envelope ever arrived (or a gate refused before spawning).
# ---------------------------------------------------------------------------

REASON_HELPER_MISSING = "helper-missing"
REASON_SPAWN_FAILED = "spawn-failed"
REASON_TIMEOUT = "timeout"
REASON_NONZERO_EXIT = "nonzero-exit"
REASON_BAD_ENVELOPE = "invalid-envelope"
REASON_BAD_RESULT = "invalid-result"
REASON_OVERSIZED_STDOUT = "oversized-stdout"
REASON_NOT_STRIPPED = "not-stripped"
REASON_GATE_REFUSED = "gate-refused"
REASON_EMPTY_ANSWER = "empty-answer"
REASON_PIPELINE_FAILED = "pipeline-failed"

# Retry taxonomy (KTD-3): ONE fresh-spawn retry for ``decoding-failure`` (also
# the symptom of an output-cap truncation) and ONE post-backoff retry for
# ``rate-limited``. Everything else — including ``context-window``, whose
# recovery (digest halving) is caller-owned — is never retried at this layer.
_RETRY_REASONS = frozenset({"decoding-failure", "rate-limited"})
_RATE_LIMIT_BACKOFF_S = 2.0

# Floor for a deadline-clamped verb timeout (KTD-7): never hand the helper a
# window so short that every call near the deadline is a guaranteed timeout.
_MIN_VERB_TIMEOUT_S = 5.0

# Module-level seam so tests can patch the rate-limit backoff.
_sleep = time.sleep


def _timeout_from_env(default: float) -> float:
    raw = os.environ.get("SCREENCAP_ONDEVICE_HELPER_TIMEOUT")
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    # A non-positive override would fail to bound the helper; fall back to the
    # default rather than trusting a bad value.
    return val if val > 0 else default


def _helper_timeout_s() -> float:
    """Per-call timeout for the legacy whole-day ``segment`` / ``answer``."""
    return _timeout_from_env(_DEFAULT_TIMEOUT_S)


def _verb_timeout_s() -> float:
    """Per-call timeout for the window-scoped verbs (shorter default)."""
    return _timeout_from_env(_DEFAULT_VERB_TIMEOUT_S)


# Env var names whose value is credential-bearing and must never reach the
# helper subprocess (defense in depth — the on-device model needs no secrets).
_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _scrubbed_env() -> dict[str, str]:
    """The process environment with credential-bearing variables removed.

    The Swift helper is trusted, bundled code, but has no need for any cloud API
    key. Stripping ``GOOGLE_GENAI_API_KEY`` and any ``*_KEY`` / ``*_TOKEN`` /
    ``*_SECRET`` / ``*_PASSWORD`` / ``*_CREDENTIAL`` keeps a compromised or
    future-extended helper from ever seeing them.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not any(m in k.upper() for m in _SECRET_ENV_MARKERS)
    }


def _find_bundled_helper() -> Path | None:
    """Return the helper inside the enclosing ``Screencap.app`` bundle, if any.

    The helper is built into the OUTER app at
    ``Screencap.app/Contents/MacOS/IntelligenceHelper`` (the app's "Embed
    IntelligenceHelper" build phase copies it there). The daemon that imports
    this module, though, runs from a NESTED helper bundle after SCR-196
    (``Screencap.app/Contents/Library/LoginItems/ScreencapDaemon.app``), whose
    own ``Contents/MacOS`` carries no helper. So we walk up through EVERY
    ``…/Contents`` ancestor and take the first that has a runnable
    ``MacOS/IntelligenceHelper`` — skipping the nested bundle's helper-less
    ``Contents`` and finding the outer app's. (The historical single-bundle
    layout — CLI at ``Contents/Resources`` — matches on its first ``Contents``,
    so this is a strict superset of the old behavior.)

    Returns ``None`` for a CLI-only / headless install (no app bundle in the
    ancestry) — the provider then reports unavailable and U7 degrades. NOTE the
    daemon launcher also exports ``SCREENCAP_ONDEVICE_HELPER`` for both the
    nested-bundle and dev-source cases, so this walk is the fallback, not the
    only path (a dev-source run has no bundle ancestry and relies on the env).
    """
    for parent in Path(__file__).resolve().parents:
        if parent.name == "Contents":
            candidate = parent / "MacOS" / _HELPER_BASENAME
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
            # Not in THIS bundle's MacOS — keep walking up. The daemon runs from
            # a nested ScreencapDaemon.app whose Contents has no helper; the one
            # it needs sits in an ANCESTOR bundle (the outer Screencap.app).
    return None


def _resolve_helper() -> Path | None:
    """Resolve the helper path: env override first, then the app bundle."""
    override = os.environ.get("SCREENCAP_ONDEVICE_HELPER")
    if override:
        p = Path(override)
        if p.is_file():
            return p
        log.warning("SCREENCAP_ONDEVICE_HELPER=%s is not a file", override)
        return None
    return _find_bundled_helper()


def _spawn_helper(
    helper: Path, payload: str, timeout_s: float,
) -> tuple[str | None, str | None]:
    """Run the helper once with ``payload`` on stdin → ``(stdout, reason)``.

    ``(stdout, None)`` on a clean exit-0 run; ``(None, generic reason)`` when
    the process could not produce output at all (timeout, spawn failure,
    non-zero exit — a non-zero exit discards stdout per the helper contract).
    """
    try:
        proc = subprocess.run(
            [str(helper)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_scrubbed_env(),
        )
    except subprocess.TimeoutExpired:
        log.warning("On-device helper timed out; unavailable")
        return None, REASON_TIMEOUT
    except OSError:
        # Vanished / not executable between resolve and spawn.
        log.warning("On-device helper could not be spawned; unavailable",
                    exc_info=True)
        return None, REASON_SPAWN_FAILED
    if proc.returncode != 0:
        log.warning(
            "On-device helper exited %d; unavailable. stderr: %s",
            proc.returncode, (proc.stderr or "").strip()[:500],
        )
        return None, REASON_NONZERO_EXIT
    return proc.stdout or "", None


def _typed_merges(result: dict) -> list[list[int]] | None:
    """Type-check an arbitrate ``result`` → ``[[int, …], …]`` or ``None``.

    Shape only (lists of real ints — bools excluded); contiguity/range
    validation over the actual window set is the caller's job (KTD-4).
    """
    merges = result.get("merges")
    if not isinstance(merges, list):
        return None
    typed: list[list[int]] = []
    for group in merges:
        if not isinstance(group, list):
            return None
        for idx in group:
            if isinstance(idx, bool) or not isinstance(idx, int):
                return None
        typed.append([int(i) for i in group])
    return typed


T = TypeVar("T")


@dataclass(frozen=True)
class CallResult(Generic[T]):
    """Outcome of one window-scoped helper verb call (SCR-275, U3).

    ``reason is None`` ⟺ success (``ok``). On failure ``value`` is ``None``
    and ``reason`` carries either a helper-reported semantic reason (the
    KTD-3 taxonomy) or one of this module's generic ``REASON_*`` strings, so
    the U4 orchestrator can route per call — e.g. ``context-window`` on a
    name-window call means "halve the digest and call again", which is
    deliberately NOT done at this layer. Note ``value`` may be falsy on
    success (an empty arbitrate merge list); test ``ok`` / ``reason``, not
    truthiness.
    """

    value: T | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.reason is None


class OnDeviceProvider:
    """Apple Foundation Models backend via a subprocess Swift helper.

    See the module docstring for the discovery, IPC envelope, and the
    fail-closed privacy contract. Holds only per-call diagnostic state
    (:attr:`last_unavailable_reason`); safe to construct per call.

    ``supports_frames`` is ``False``: the on-device helper takes text only, so
    any ``masked_frames`` handed to :meth:`segment` / :meth:`answer` are ignored
    — graceful omission, never a raise (SCR-272, U4).
    """

    supports_frames: bool = False

    def __init__(
        self,
        *,
        recording_dir: "Path | str | None" = None,
        stop_event: "object | None" = None,
        is_live: bool = False,
        manifests: "list[dict] | None" = None,
    ) -> None:
        #: Out-of-band diagnostic (KTD-1): WHY the most recent ``segment`` /
        #: ``answer`` / ``call_*`` invocation was unavailable; ``None`` after
        #: a full success (including segment's ran-but-no-usable-tasks
        #: ``None``). After a PARTIAL windowed pass (a tasks dict mixing model
        #: and mechanical sources) it carries the dominant per-window failure
        #: reason — the U6 honesty seam (KTD-8). Read via ``getattr`` by the
        #: chained provider / terminal stage — never part of the return value,
        #: which stays the identity-compared :data:`PROVIDER_UNAVAILABLE`
        #: singleton.
        self.last_unavailable_reason: str | None = None
        # SCR-275 U4 (KTD-1 transport): optional per-recording context. When
        # ``recording_dir`` is present (the terminal stage supplies it at call
        # time via ``build_day_split_provider``) AND the recording has chunk
        # manifests, ``segment`` runs the heuristic-first windowed pipeline
        # instead of the legacy whole-day helper call. ``manifests`` are the
        # caller's already-loaded chunk manifests (``None`` → ``segment``
        # loads its own from disk). Legacy callers that construct the provider
        # bare keep the old behavior unchanged.
        self._recording_dir = Path(recording_dir) if recording_dir else None
        self._stop_event = stop_event
        self._is_live = is_live
        self._manifests = manifests

    def segment(
        self,
        activity_summary: dict,
        *,
        masked_frames: "tuple[MaskedFrame, ...]" = (),
    ) -> dict | None | ProviderUnavailable:
        """Segment the session on-device via the Swift helper.

        See :mod:`screencap.segmentation.provider` for the tri-state return.
        ``masked_frames`` is accepted but ignored (graceful omission).
        """
        # Fail-closed privacy gate (R11): refuse anything not explicitly marked
        # as privacy-stripped. Do NOT spawn the helper on unmarked input.
        if activity_summary.get("stripped") is not True:
            log.warning(
                "OnDeviceProvider refused an activity summary not marked "
                "stripped=True (fail-closed); returning unavailable."
            )
            self.last_unavailable_reason = REASON_NOT_STRIPPED
            return PROVIDER_UNAVAILABLE

        helper = _resolve_helper()
        if helper is None:
            log.info("On-device helper not found (CLI-only install?); unavailable")
            self.last_unavailable_reason = REASON_HELPER_MISSING
            return PROVIDER_UNAVAILABLE

        # SCR-275 U4 (KTD-1): with per-recording context AND on-disk chunk
        # manifests, the heuristic-first windowed pipeline replaces the legacy
        # whole-day call — same tri-state exterior. No manifests (legacy /
        # single-file recording) → fall through to the whole-day path.
        if self._recording_dir is not None:
            manifests = (
                self._manifests if self._manifests is not None
                else self._load_manifests()
            )
            if manifests:
                return self._segment_windowed(activity_summary, manifests)

        payload = json.dumps(activity_summary["summary"])

        stdout, spawn_reason = _spawn_helper(helper, payload, _helper_timeout_s())
        if stdout is None:
            self.last_unavailable_reason = spawn_reason
            return PROVIDER_UNAVAILABLE

        result, reason = self._parse_envelope(stdout)
        if result is None:
            # Either an explicit "unavailable" status from the helper (OS < 26,
            # Apple Intelligence off, model not ready) or an unparseable
            # envelope — both map to the sentinel, with the reason recorded.
            self.last_unavailable_reason = reason
            return PROVIDER_UNAVAILABLE
        self.last_unavailable_reason = None

        # ``result`` is the raw tasks dict — run it through the shared repair/
        # reject validator (same net as cloud). A crash inside validation (e.g.
        # a malformed task item) is swallowed to ``None`` — the helper *ran*, so
        # this is a "no usable tasks" outcome, not "could not run".
        try:
            return validate_llm_tasks(
                result,
                activity_summary["session_start"],
                activity_summary["session_end"],
                activity_summary["time_map"],
            )
        except Exception:
            log.warning("On-device helper output failed validation", exc_info=True)
            return None

    def _load_manifests(self) -> list[dict]:
        """The recording's on-disk chunk manifests; ``[]`` fails open to legacy."""
        try:
            from screencap.segmentation.local_source import load_local_manifests

            return load_local_manifests(self._recording_dir)
        except Exception:  # noqa: BLE001 — viability probe, never a crash
            log.debug("manifest load for windowed segmentation failed",
                      exc_info=True)
            return []

    def _segment_windowed(
        self, activity_summary: dict, manifests: list[dict],
    ) -> dict | None | ProviderUnavailable:
        """Run the SCR-275 heuristic-first pipeline (U4) for this recording.

        The pipeline drives this provider's ``call_arbitrate`` /
        ``call_name_window`` / ``call_day_summary`` verbs and records the pass
        outcome on :attr:`last_unavailable_reason`. Any unexpected error maps
        to the sentinel (with :data:`REASON_PIPELINE_FAILED`) so the caller's
        degrade ladder still routes — never a raise.
        """
        try:
            from screencap.segmentation.local_source import LocalActivitySource
            from screencap.segmentation.ondevice_pipeline import (
                compute_pinned_before_ts,
                run_heuristic_pipeline,
            )

            pinned = (
                compute_pinned_before_ts(self._recording_dir)
                if self._is_live else None
            )
            return run_heuristic_pipeline(
                self,
                self._recording_dir,
                LocalActivitySource(self._recording_dir, manifests),
                manifests,
                session_start=float(activity_summary.get("session_start") or 0.0),
                session_end=float(activity_summary.get("session_end") or 0.0),
                is_live=self._is_live,
                stop_event=self._stop_event,
                pinned_before_ts=pinned,
            )
        except Exception:  # noqa: BLE001 — the tri-state must hold, never a raise
            log.warning(
                "heuristic-first on-device pipeline failed; unavailable",
                exc_info=True,
            )
            self.last_unavailable_reason = REASON_PIPELINE_FAILED
            return PROVIDER_UNAVAILABLE

    @staticmethod
    def _parse_envelope(stdout: str) -> tuple[dict | None, str | None]:
        """Decode the helper's stdout envelope → ``(result, reason)``.

        The helper prints one JSON object:
        ``{"status": "ok", "result": {…}}`` or
        ``{"status": "unavailable", "reason": "…"}``.

        Returns:
        - ``(result_dict, None)`` when ``status == "ok"`` with a dict result.
        - ``(None, reason)`` when ``status == "unavailable"`` — ``reason`` is
          the helper's own semantic string (KTD-3), or
          :data:`REASON_BAD_ENVELOPE` if the helper sent none.
        - ``(None, REASON_BAD_ENVELOPE)`` when stdout can't be parsed as the
          expected envelope at all (garbage output → a reason, never a crash).
        """
        text = (stdout or "").strip()
        if not text:
            log.warning("On-device helper produced empty stdout; unavailable")
            return None, REASON_BAD_ENVELOPE
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            log.warning("On-device helper stdout was not JSON; unavailable")
            return None, REASON_BAD_ENVELOPE

        if not isinstance(envelope, dict):
            log.warning("On-device helper envelope was not an object; unavailable")
            return None, REASON_BAD_ENVELOPE

        status = envelope.get("status")
        if status == "unavailable":
            reason = envelope.get("reason")
            log.info("On-device helper reported unavailable: %s", reason or "")
            if isinstance(reason, str) and reason:
                return None, reason
            return None, REASON_BAD_ENVELOPE
        if status == "ok":
            result = envelope.get("result")
            if isinstance(result, dict):
                return result, None
            log.warning("On-device helper 'ok' envelope had no result dict")
            return None, REASON_BAD_ENVELOPE

        log.warning("On-device helper envelope had unknown status %r", status)
        return None, REASON_BAD_ENVELOPE

    # -- Window-scoped verbs (SCR-275, U3 / KTD-2, KTD-3) -------------------

    def call_arbitrate(
        self, window_lines: list[str], *, stripped: bool,
    ) -> CallResult[list[list[int]]]:
        """One arbitration call: which contiguous candidate windows to merge.

        ``window_lines`` are the compressed one-line-per-window arbitration
        forms (``WindowDigest.arbitration_line``); this wrapper indexes them
        into the helper's ``{"task":"arbitrate","windows":[{"i":…,"line":…}]}``
        request. ``stripped`` is the caller's R11 attestation that every line
        derives from privacy-stripped digests — anything but ``True`` is
        refused WITHOUT spawning the helper (fail-closed, mirroring
        :meth:`segment`).

        Success value: ``[[int, …], …]`` merge groups (empty = keep every
        heuristic boundary). Contiguity/range validation of the groups is the
        caller's job (KTD-4). Failure: ``value is None`` with the reason.
        """
        if stripped is not True:
            log.warning(
                "call_arbitrate refused lines not attested stripped "
                "(fail-closed); not spawning the helper."
            )
            self.last_unavailable_reason = REASON_NOT_STRIPPED
            return CallResult(None, REASON_NOT_STRIPPED)

        request = {
            "task": "arbitrate",
            "windows": [
                {"i": i, "line": str(line)} for i, line in enumerate(window_lines)
            ],
        }
        result, reason = self._call_verb(request)
        if result is None:
            self.last_unavailable_reason = reason
            return CallResult(None, reason)

        merges = _typed_merges(result)
        if merges is None:
            log.warning("On-device arbitrate result had a malformed merges list")
            self.last_unavailable_reason = REASON_BAD_RESULT
            return CallResult(None, REASON_BAD_RESULT)
        self.last_unavailable_reason = None
        return CallResult(merges, None)

    def call_name_window(self, digest_payload: dict) -> CallResult[tuple[str, str]]:
        """One naming call for a single window digest → ``(name, category)``.

        ``digest_payload`` is the U1 ``WindowDigest.payload`` as wrapped by
        the U4 orchestrator: the trimmed digest content plus a
        ``"stripped": True`` attestation. The marker is asserted HERE
        (fail-closed — anything but exactly ``True`` is refused without
        spawning, mirroring :meth:`segment`) and then dropped from the
        ``{"task":"name-window","digest":{…}}`` request, so it never rides
        into the model prompt.

        A ``context-window`` failure is surfaced, NOT handled: digest halving
        is caller-driven (the U4 orchestrator halves and calls again, at most
        three times, then goes mechanical — KTD-3). This layer never mutates
        the digest.
        """
        if not isinstance(digest_payload, dict) or digest_payload.get(
            "stripped"
        ) is not True:
            log.warning(
                "call_name_window refused a digest not marked stripped=True "
                "(fail-closed); not spawning the helper."
            )
            self.last_unavailable_reason = REASON_NOT_STRIPPED
            return CallResult(None, REASON_NOT_STRIPPED)

        digest = {k: v for k, v in digest_payload.items() if k != "stripped"}
        result, reason = self._call_verb({"task": "name-window", "digest": digest})
        if result is None:
            self.last_unavailable_reason = reason
            return CallResult(None, reason)

        name, category = result.get("name"), result.get("category")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(category, str)
        ):
            log.warning("On-device name-window result had no usable name/category")
            self.last_unavailable_reason = REASON_BAD_RESULT
            return CallResult(None, REASON_BAD_RESULT)
        self.last_unavailable_reason = None
        return CallResult((name.strip(), category.strip()), None)

    def call_day_summary(self, task_rows: list[dict]) -> CallResult[dict]:
        """One day-summary call over the named-task list → overview + tags.

        ``task_rows`` are ``{"name": …, "category": …, "minutes": …}`` rows.
        Deliberately NO strip gate here: the inputs are model outputs and
        mechanical labels — already on the far side of the strip chokepoint —
        never raw screen content, so there is nothing to attest (KTD-5's gate
        covers payloads that carry screen-derived text; these don't).

        Success value: ``{"overview": str, "tags": [str, …]}`` (untrusted
        model output — the caller applies the existing tag regex/count
        hygiene per KTD-9 before persisting).
        """
        request = {"task": "day-summary", "tasks": list(task_rows)}
        result, reason = self._call_verb(request)
        if result is None:
            self.last_unavailable_reason = reason
            return CallResult(None, reason)

        overview, tags = result.get("overview"), result.get("tags")
        if (
            not isinstance(overview, str)
            or not overview.strip()
            or not isinstance(tags, list)
            or not all(isinstance(t, str) for t in tags)
        ):
            log.warning("On-device day-summary result had no usable overview/tags")
            self.last_unavailable_reason = REASON_BAD_RESULT
            return CallResult(None, REASON_BAD_RESULT)
        self.last_unavailable_reason = None
        return CallResult({"overview": overview.strip(), "tags": list(tags)}, None)

    def call_block_bullets(
        self, digest_payload: dict,
    ) -> CallResult[list[str]]:
        """One diary-bullet call for a merged block's evidence digest → ``[str, …]``.

        The prose-kind (U3, KTD-4) analogue of :meth:`call_name_window`: it takes
        the block's small evidence digest (fragment work-names / categories /
        minutes / apps — derived from already-stripped per-window evidence, never
        raw screen content) as wrapped by the consolidator with a
        ``"stripped": True`` attestation, asserted HERE fail-closed (anything but
        exactly ``True`` is refused without spawning, mirroring
        :meth:`call_name_window`) and then dropped from the
        ``{"task":"block-bullets","digest":{…}}`` request so it never rides into
        the prompt. The digest carries NO downstream block name, so a bullet can
        never assert content a confidence gate blanked (KTD-4).

        A ``context-window`` failure is surfaced, NOT handled: digest halving is
        caller-driven (the consolidator halves and calls again, then falls to the
        app-level heuristic). Success value is the raw ``list[str]`` of bullets —
        untrusted model output; the caller runs it through
        :func:`~screencap.segmentation.sanitize.sanitize_bullets` before it is
        persisted.
        """
        if not isinstance(digest_payload, dict) or digest_payload.get(
            "stripped"
        ) is not True:
            log.warning(
                "call_block_bullets refused a digest not marked stripped=True "
                "(fail-closed); not spawning the helper."
            )
            self.last_unavailable_reason = REASON_NOT_STRIPPED
            return CallResult(None, REASON_NOT_STRIPPED)

        digest = {k: v for k, v in digest_payload.items() if k != "stripped"}
        result, reason = self._call_verb(
            {"task": "block-bullets", "digest": digest}
        )
        if result is None:
            self.last_unavailable_reason = reason
            return CallResult(None, reason)

        bullets = result.get("bullets")
        if not isinstance(bullets, list) or not all(
            isinstance(b, str) for b in bullets
        ):
            log.warning("On-device block-bullets result had no usable bullet list")
            self.last_unavailable_reason = REASON_BAD_RESULT
            return CallResult(None, REASON_BAD_RESULT)
        self.last_unavailable_reason = None
        return CallResult([b.strip() for b in bullets if b.strip()], None)

    def _call_verb(
        self, request: dict, *, deadline: "float | None" = None,
    ) -> tuple[dict | None, str | None]:
        """Spawn the helper for one window-scoped verb → ``(result, reason)``.

        Shares the legacy spawn path (discovery, scrubbed env, stdout cap)
        with the shorter per-verb timeout, and applies the KTD-3 retry
        taxonomy: one fresh-spawn retry for ``decoding-failure``, one
        post-backoff retry for ``rate-limited``; nothing else retries.

        ``deadline`` (KTD-7, optional): a ``time.monotonic()`` timestamp — the
        caller's pass budget. Past the deadline the retry (and its backoff
        sleep) is skipped, and each spawn's timeout is clamped to the
        remaining budget (floored at :data:`_MIN_VERB_TIMEOUT_S`) so one slow
        verb cannot blow far past the pass budget.
        """
        helper = _resolve_helper()
        if helper is None:
            log.info("On-device helper not found (CLI-only install?); unavailable")
            return None, REASON_HELPER_MISSING

        payload = json.dumps(request)
        result, reason = self._spawn_and_parse(helper, payload, deadline=deadline)
        if reason in _RETRY_REASONS:
            if deadline is not None and time.monotonic() > deadline:
                log.info(
                    "On-device helper %s failed (%s) past the pass deadline; "
                    "not retrying", request.get("task"), reason,
                )
                return result, reason
            if reason == "rate-limited":
                _sleep(_RATE_LIMIT_BACKOFF_S)
            log.info(
                "On-device helper %s failed (%s); retrying once",
                request.get("task"), reason,
            )
            result, reason = self._spawn_and_parse(
                helper, payload, deadline=deadline,
            )
        return result, reason

    def _spawn_and_parse(
        self, helper: Path, payload: str, *, deadline: "float | None" = None,
    ) -> tuple[dict | None, str | None]:
        timeout_s = _verb_timeout_s()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            timeout_s = min(timeout_s, max(_MIN_VERB_TIMEOUT_S, remaining))
        stdout, reason = _spawn_helper(helper, payload, timeout_s)
        if stdout is None:
            return None, reason
        if len(stdout) > _MAX_ANSWER_STDOUT_BYTES:
            log.warning("On-device verb stdout exceeds the size cap; unavailable")
            return None, REASON_OVERSIZED_STDOUT
        return self._parse_envelope(stdout)

    # -- Free-form generation path (SCR-243, U4) ---------------------------

    def answer(
        self,
        prompt: str,
        evidence: Evidence,
        *,
        masked_frames: "tuple[MaskedFrame, ...]" = (),
    ) -> str | ProviderUnavailable:
        """Answer ``prompt`` grounded in ``evidence`` on-device via the Swift helper.

        See :mod:`screencap.segmentation.generation` for the two-state
        (``str`` | :data:`PROVIDER_UNAVAILABLE`) return. Never raises for an
        ordinary error. The grounding instructions live in the helper (KTD3);
        this side passes the raw prompt + stripped evidence text. ``masked_frames``
        is accepted but ignored (graceful omission).
        """
        # Single fail-closed gate: stripped marker (R11), str text/prompt (R12),
        # within the size caps (KTD10). No helper spawn on refusal.
        if not evidence_gate_ok(prompt, evidence):
            log.warning("OnDeviceProvider.answer refused the request (gate); unavailable")
            self.last_unavailable_reason = REASON_GATE_REFUSED
            return PROVIDER_UNAVAILABLE

        helper = _resolve_helper()
        if helper is None:
            log.info("On-device helper not found (CLI-only install?); unavailable")
            self.last_unavailable_reason = REASON_HELPER_MISSING
            return PROVIDER_UNAVAILABLE

        payload = json.dumps(
            {"task": "answer", "prompt": prompt, "evidence": evidence.text}
        )

        stdout, spawn_reason = _spawn_helper(helper, payload, _helper_timeout_s())
        if stdout is None:
            self.last_unavailable_reason = spawn_reason
            return PROVIDER_UNAVAILABLE

        if len(stdout) > _MAX_ANSWER_STDOUT_BYTES:
            log.warning("On-device answer stdout exceeds the size cap; unavailable")
            self.last_unavailable_reason = REASON_OVERSIZED_STDOUT
            return PROVIDER_UNAVAILABLE

        text, reason = self._parse_text_envelope(stdout)
        if text is None:
            self.last_unavailable_reason = reason
            return PROVIDER_UNAVAILABLE

        cleaned = sanitize_answer(text)
        if not cleaned.strip():
            # Empty/whitespace answer is indistinguishable from "no answer" —
            # treat it as unavailable rather than returning a blank string (KTD9).
            self.last_unavailable_reason = REASON_EMPTY_ANSWER
            return PROVIDER_UNAVAILABLE
        self.last_unavailable_reason = None
        return cleaned

    @staticmethod
    def _parse_text_envelope(stdout: str) -> tuple[str | None, str | None]:
        """Decode the helper's stdout envelope for the answer path.

        ``(text, None)`` for ``{"status":"ok","result":"<text>"}``;
        ``(None, reason)`` for ``{"status":"unavailable","reason":…}`` (the
        helper's semantic reason, or :data:`REASON_BAD_ENVELOPE` if absent);
        ``(None, REASON_BAD_ENVELOPE)`` for a non-string ``result`` or any
        unparseable output.
        """
        raw = (stdout or "").strip()
        if not raw:
            log.warning("On-device answer helper produced empty stdout; unavailable")
            return None, REASON_BAD_ENVELOPE
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.warning("On-device answer helper stdout was not JSON; unavailable")
            return None, REASON_BAD_ENVELOPE
        if not isinstance(envelope, dict):
            return None, REASON_BAD_ENVELOPE
        if envelope.get("status") == "ok":
            result = envelope.get("result")
            if isinstance(result, str):
                return result, None
            log.warning("On-device answer 'ok' envelope had no string result")
            return None, REASON_BAD_ENVELOPE
        reason = envelope.get("reason")
        log.info("On-device answer helper reported unavailable: %s", reason or "")
        if isinstance(reason, str) and reason:
            return None, reason
        return None, REASON_BAD_ENVELOPE
