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
1. ``SCREENCAP_ONDEVICE_HELPER`` env var (an explicit path) — used first;
   primarily a test seam (point it at a fake helper) but also a valid override.
2. The bundled helper inside the macOS app
   (``…/ScreenCap.app/Contents/MacOS/IntelligenceHelper``), discovered relative
   to this file when running from a source/dev checkout is NOT attempted — the
   helper ships only in the built app. **CLI-only / headless installs have no
   app bundle and therefore no helper**, so discovery fails and the provider
   reports unavailable — exactly the case U7 degrades (heuristic for
   day-splitting; consented cloud for summaries).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable
from screencap.segmentation.validate import validate_llm_tasks

log = logging.getLogger(__name__)

# The helper binary name inside the app bundle's MacOS dir.
_HELPER_BASENAME = "IntelligenceHelper"

# Wall-clock ceiling for one helper invocation. The on-device model is slow but
# bounded; a hang past this maps to unavailable so terminal_stage never blocks
# on segmentation. Overridable via env for the (fast) fake-helper tests.
_DEFAULT_TIMEOUT_S = 120.0


def _helper_timeout_s() -> float:
    raw = os.environ.get("SCREENCAP_ONDEVICE_HELPER_TIMEOUT")
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    # A non-positive override would fail to bound the helper; fall back to the
    # default rather than trusting a bad value.
    return val if val > 0 else _DEFAULT_TIMEOUT_S


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
    """Return the helper inside the enclosing ``ScreenCap.app`` bundle, if any.

    The embedded CLI lives at ``ScreenCap.app/Contents/Resources/…`` and the
    helper is built into ``ScreenCap.app/Contents/MacOS/IntelligenceHelper``.
    We walk up from this module looking for a ``…/Contents`` dir with a
    sibling ``MacOS/IntelligenceHelper``. Returns ``None`` for a CLI-only /
    headless install (no app bundle) — the provider then reports unavailable.
    """
    for parent in Path(__file__).resolve().parents:
        if parent.name == "Contents":
            candidate = parent / "MacOS" / _HELPER_BASENAME
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
            return None
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


class OnDeviceProvider:
    """Apple Foundation Models backend via a subprocess Swift helper.

    See the module docstring for the discovery, IPC envelope, and the
    fail-closed privacy contract. Stateless; safe to construct per call.
    """

    def segment(self, activity_summary: dict) -> dict | None | ProviderUnavailable:
        """Segment the session on-device via the Swift helper.

        See :mod:`screencap.segmentation.provider` for the tri-state return.
        """
        # Fail-closed privacy gate (R11): refuse anything not explicitly marked
        # as privacy-stripped. Do NOT spawn the helper on unmarked input.
        if activity_summary.get("stripped") is not True:
            log.warning(
                "OnDeviceProvider refused an activity summary not marked "
                "stripped=True (fail-closed); returning unavailable."
            )
            return PROVIDER_UNAVAILABLE

        helper = _resolve_helper()
        if helper is None:
            log.info("On-device helper not found (CLI-only install?); unavailable")
            return PROVIDER_UNAVAILABLE

        payload = json.dumps(activity_summary["summary"])

        try:
            proc = subprocess.run(
                [str(helper)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=_helper_timeout_s(),
                env=_scrubbed_env(),
            )
        except subprocess.TimeoutExpired:
            log.warning("On-device helper timed out; unavailable")
            return PROVIDER_UNAVAILABLE
        except OSError:
            # Vanished / not executable between resolve and spawn.
            log.warning("On-device helper could not be spawned; unavailable",
                        exc_info=True)
            return PROVIDER_UNAVAILABLE

        if proc.returncode != 0:
            log.warning(
                "On-device helper exited %d; unavailable. stderr: %s",
                proc.returncode, (proc.stderr or "").strip()[:500],
            )
            return PROVIDER_UNAVAILABLE

        envelope = self._parse_envelope(proc.stdout)
        if envelope is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE
        if envelope is None:
            # Parsed an explicit "unavailable" status from the helper (OS < 26,
            # Apple Intelligence off, model not ready).
            return PROVIDER_UNAVAILABLE

        # ``envelope`` is the raw tasks dict — run it through the shared repair/
        # reject validator (same net as cloud). A crash inside validation (e.g.
        # a malformed task item) is swallowed to ``None`` — the helper *ran*, so
        # this is a "no usable tasks" outcome, not "could not run".
        try:
            return validate_llm_tasks(
                envelope,
                activity_summary["session_start"],
                activity_summary["session_end"],
                activity_summary["time_map"],
            )
        except Exception:
            log.warning("On-device helper output failed validation", exc_info=True)
            return None

    @staticmethod
    def _parse_envelope(stdout: str) -> dict | None | ProviderUnavailable:
        """Decode the helper's stdout envelope.

        The helper prints one JSON object:
        ``{"status": "ok", "result": {tasks…}}`` or
        ``{"status": "unavailable", "reason": "…"}``.

        Returns:
        - the raw ``result`` **dict** when ``status == "ok"`` (to be validated).
        - ``None`` when ``status == "unavailable"`` (a clean model-off signal —
          the caller maps this to :data:`PROVIDER_UNAVAILABLE`).
        - :data:`PROVIDER_UNAVAILABLE` when stdout can't be parsed as the
          expected envelope at all (garbage output → treat as unavailable, not a
          crash).
        """
        text = (stdout or "").strip()
        if not text:
            log.warning("On-device helper produced empty stdout; unavailable")
            return PROVIDER_UNAVAILABLE
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            log.warning("On-device helper stdout was not JSON; unavailable")
            return PROVIDER_UNAVAILABLE

        if not isinstance(envelope, dict):
            log.warning("On-device helper envelope was not an object; unavailable")
            return PROVIDER_UNAVAILABLE

        status = envelope.get("status")
        if status == "unavailable":
            log.info("On-device helper reported unavailable: %s",
                     envelope.get("reason", ""))
            return None
        if status == "ok":
            result = envelope.get("result")
            if isinstance(result, dict):
                return result
            log.warning("On-device helper 'ok' envelope had no result dict")
            return PROVIDER_UNAVAILABLE

        log.warning("On-device helper envelope had unknown status %r", status)
        return PROVIDER_UNAVAILABLE
