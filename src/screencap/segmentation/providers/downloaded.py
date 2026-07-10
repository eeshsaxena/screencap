"""Downloaded local-model backend for the provider interface (U2, SCR-239).

Runs an opt-in downloaded ~3B model (MLX on Apple Silicon, llama.cpp on Intel)
by spawning a **hardened subprocess worker**
(:mod:`screencap.segmentation.local_model.worker`) so the ~2 GB model never stays
resident in the all-day daemon and a crash/OOM is isolated (KTD3). It mirrors the
Apple Foundation Models :class:`~screencap.segmentation.providers.ondevice.OnDeviceProvider`
subprocess contract, then hardens it because the worker runs
attacker-influenceable (recording-derived) content through a model:

- **fail-closed privacy gate (R10):** refuses any activity summary not marked
  ``stripped=True`` — WITHOUT spawning the worker.
- **allowlist env + offline:** the worker launches with a minimal env (no daemon
  secrets — stronger than the on-device substring denylist) and ``HF_HUB_OFFLINE``
  so it can never phone home.
- **CPU de-prioritized:** ``nice`` so segmentation never starves capture.
- **size caps:** an oversized stdin summary is rejected before spawn; an oversized
  stdout envelope is rejected rather than parsed.
- **stderr not logged verbatim:** it can carry recording-derived text.
- **untrusted output:** validated tasks pass through the KTD12 sanitizer before
  return (the U3 confidence gate is wired in on top of this).

Return contract (see :mod:`screencap.segmentation.provider`)
------------------------------------------------------------
- a validated **tasks dict** — the worker ran and the output validated.
- ``None`` — the worker ran but produced nothing usable.
- :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` — could not run at
  all (no model installed, worker missing, refused unmarked input, timeout, crash,
  unparseable envelope). U7/U8's degradation ladder routes on this sentinel.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from screencap.segmentation.generation import Evidence
from screencap.segmentation.generation_finish import (
    build_answer_prompt,
    evidence_gate_ok,
    sanitize_answer,
)
from screencap.segmentation.local_finish import build_local_prompt, finalize_local_result
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable

log = logging.getLogger(__name__)

# Env override pointing at a replacement worker command (primarily a test seam —
# a fake worker — but also a valid override). Mirrors SCREENCAP_ONDEVICE_HELPER.
_WORKER_OVERRIDE_ENV = "SCREENCAP_DOWNLOADED_WORKER"

# Env override for the model path (used before the U4 registry lands, and as a
# valid override / test seam).
_MODEL_PATH_ENV = "SCREENCAP_LOCAL_MODEL_PATH"

# Phased wall-clock budget (KTD3): a load budget + a generation budget, summed
# into the subprocess ceiling. Cold-loading ~2 GB from disk is the dominant cost,
# so this is NOT the resident-AFM 120 s blanket.
_LOAD_BUDGET_S = 60.0
_GENERATION_BUDGET_S = 90.0

# Reject a stdin summary larger than this before spawning (DoS / OOM guard).
_MAX_STDIN_BYTES = 512 * 1024

# Reject a stdout envelope larger than this rather than parsing it.
_MAX_STDOUT_BYTES = 1 * 1024 * 1024

# `nice` increment for the worker so segmentation yields to capture.
_NICE_INCREMENT = 10

# Env keys the worker legitimately needs (non-secret). Everything else — notably
# any `*_KEY` / `*_TOKEN` / `*_SECRET` — is dropped by construction (allowlist).
_ALLOWLIST_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "HF_HOME",
    "HF_HUB_CACHE",
)


def _worker_timeout_s() -> float:
    """The summed load+generation wall-clock ceiling (env-overridable for tests)."""
    raw = os.environ.get("SCREENCAP_DOWNLOADED_WORKER_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return _LOAD_BUDGET_S + _GENERATION_BUDGET_S


def _allowlist_env() -> dict[str, str]:
    """A minimal env for the worker: allowlisted non-secret keys + offline flags.

    Stronger than the on-device ``_scrubbed_env`` denylist — a future secret env
    var named without a KEY/TOKEN marker would leak through a denylist but cannot
    here, and the worker is forced offline so it can never fetch at inference time.
    """
    env = {k: os.environ[k] for k in _ALLOWLIST_ENV_KEYS if k in os.environ}
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def _resolve_model_path() -> str | None:
    """Resolve the installed model path: env override first, then the U4 registry.

    Returns ``None`` when no model is installed (→ provider reports unavailable).
    """
    override = os.environ.get(_MODEL_PATH_ENV)
    if override:
        return override if Path(override).exists() else None
    try:
        from screencap.models import get_installed_model_path
    except ImportError:
        return None  # U4 registry not present yet
    path = get_installed_model_path()
    return str(path) if path else None


def _installed_model_size() -> int | None:
    """The disclosed on-disk size of the installed model, for the RAM floor (KTD11)."""
    try:
        from screencap.models import get_disclosed_size

        return get_disclosed_size()
    except ImportError:
        return None


def _worker_command() -> list[str]:
    override = os.environ.get(_WORKER_OVERRIDE_ENV)
    if override:
        return [override]
    return [sys.executable, "-m", "screencap.segmentation.local_model.worker"]


class DownloadedProvider:
    """Downloaded local model via a hardened subprocess worker. See module docs."""

    def segment(self, activity_summary: dict) -> dict | None | ProviderUnavailable:
        # Fail-closed privacy gate (R10): refuse unmarked input, no worker spawn.
        if activity_summary.get("stripped") is not True:
            log.warning(
                "DownloadedProvider refused an activity summary not marked "
                "stripped=True (fail-closed); returning unavailable."
            )
            return PROVIDER_UNAVAILABLE

        model_path = _resolve_model_path()
        if model_path is None:
            log.info("No downloaded model installed; provider unavailable")
            return PROVIDER_UNAVAILABLE

        try:
            prompt = build_local_prompt(activity_summary["summary"])
        except Exception:  # pragma: no cover - defensive
            log.warning("Failed to build the segmentation prompt", exc_info=True)
            return PROVIDER_UNAVAILABLE

        payload = json.dumps({"prompt": prompt, "model_path": model_path})
        if len(payload.encode("utf-8")) > _MAX_STDIN_BYTES:
            log.warning("Downloaded-model request exceeds the stdin cap; unavailable")
            return PROVIDER_UNAVAILABLE

        raw_result = self._run_worker(payload)
        if raw_result is PROVIDER_UNAVAILABLE or raw_result is None:
            return raw_result  # unavailable (could not run)

        # ``raw_result`` is the worker's raw tasks dict — validate → sanitize
        # (KTD12) → confidence-gate (KTD9), shared with the BYO backend.
        return finalize_local_result(raw_result, activity_summary)

    def _run_worker(self, payload: str) -> dict | None | ProviderUnavailable:
        """Spawn the worker and return its raw ``result`` dict, ``None``, or the sentinel.

        Guarded by the process-wide single-flight + RAM-headroom precheck (KTD11):
        a second overlapping worker, or a machine short on memory, skips fail-open
        to ``PROVIDER_UNAVAILABLE`` (→ heuristic) rather than thrashing capture.
        """
        from screencap.segmentation.inference_guard import (
            has_ram_headroom,
            inference_slot,
        )

        with inference_slot() as acquired:
            if not acquired:
                log.info("Another inference worker is in flight; skipping (unavailable)")
                return PROVIDER_UNAVAILABLE
            if not has_ram_headroom(_installed_model_size()):
                return PROVIDER_UNAVAILABLE
            return self._spawn_worker(payload)

    def _spawn_worker(self, payload: str) -> dict | None | ProviderUnavailable:
        cmd = _worker_command()

        def _preexec() -> None:  # pragma: no cover - child-side, POSIX only
            try:
                os.nice(_NICE_INCREMENT)
            except OSError:
                pass

        try:
            proc = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_worker_timeout_s(),
                env=_allowlist_env(),
                preexec_fn=_preexec if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired:
            log.warning("Downloaded-model worker timed out; unavailable")
            return PROVIDER_UNAVAILABLE
        except OSError:
            log.warning("Downloaded-model worker could not be spawned; unavailable")
            return PROVIDER_UNAVAILABLE

        if proc.returncode != 0:
            # Do NOT log stderr verbatim — it can carry recording-derived text.
            log.warning("Downloaded-model worker exited %d; unavailable", proc.returncode)
            return PROVIDER_UNAVAILABLE

        if len(proc.stdout or "") > _MAX_STDOUT_BYTES:
            log.warning("Downloaded-model worker output exceeds the stdout cap; unavailable")
            return PROVIDER_UNAVAILABLE

        return self._parse_envelope(proc.stdout)

    @staticmethod
    def _parse_envelope(stdout: str) -> dict | None | ProviderUnavailable:
        """Decode the worker's ``{status, result}`` envelope into the raw tasks dict.

        Returns the raw ``result`` dict on ``status == "ok"`` (to be validated),
        or :data:`PROVIDER_UNAVAILABLE` on ``status == "unavailable"`` or any
        unparseable/garbage output.
        """
        text = (stdout or "").strip()
        if not text:
            log.warning("Downloaded-model worker produced empty stdout; unavailable")
            return PROVIDER_UNAVAILABLE
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            log.warning("Downloaded-model worker stdout was not JSON; unavailable")
            return PROVIDER_UNAVAILABLE
        if not isinstance(envelope, dict):
            return PROVIDER_UNAVAILABLE

        status = envelope.get("status")
        if status == "ok":
            result = envelope.get("result")
            return result if isinstance(result, dict) else PROVIDER_UNAVAILABLE
        if status == "unavailable":
            log.info("Downloaded-model worker unavailable: %s", envelope.get("reason", ""))
            return PROVIDER_UNAVAILABLE
        return PROVIDER_UNAVAILABLE

    # -- Free-form generation path (SCR-243, U10) --------------------------

    def answer(self, prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
        """Answer ``prompt`` grounded in ``evidence`` via the downloaded model's
        free-form text mode.

        Reuses the hardened subprocess machinery (allowlist env, single-flight,
        RAM precheck, ``nice``, size caps) but a **grammar-free** worker mode
        (``mode="generate_text"``, KTD8) that returns raw text — not the JSON
        tasks envelope. Returns the sanitized answer or
        :data:`PROVIDER_UNAVAILABLE`. Never raises.
        """
        # Single fail-closed gate: stripped marker (R10/R11), str text/prompt
        # (R12), within the size caps (KTD10). No worker spawn on refusal.
        if not evidence_gate_ok(prompt, evidence):
            log.warning("DownloadedProvider.answer refused the request (gate); unavailable")
            return PROVIDER_UNAVAILABLE

        model_path = _resolve_model_path()
        if model_path is None:
            log.info("No downloaded model installed; answer unavailable")
            return PROVIDER_UNAVAILABLE

        payload = json.dumps(
            {
                "mode": "generate_text",
                "prompt": build_answer_prompt(prompt, evidence),
                "model_path": model_path,
            }
        )
        if len(payload.encode("utf-8")) > _MAX_STDIN_BYTES:
            log.warning("Downloaded-model answer request exceeds the stdin cap; unavailable")
            return PROVIDER_UNAVAILABLE

        raw = self._run_answer_worker(payload)
        if raw is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE
        cleaned = sanitize_answer(raw)  # type: ignore[arg-type]
        if not cleaned.strip():
            return PROVIDER_UNAVAILABLE
        return cleaned

    def _run_answer_worker(self, payload: str) -> str | ProviderUnavailable:
        """Single-flight + RAM-headroom guarded spawn for the answer path."""
        from screencap.segmentation.inference_guard import (
            has_ram_headroom,
            inference_slot,
        )

        with inference_slot() as acquired:
            if not acquired:
                log.info("Another inference worker is in flight; skipping (unavailable)")
                return PROVIDER_UNAVAILABLE
            if not has_ram_headroom(_installed_model_size()):
                return PROVIDER_UNAVAILABLE
            return self._spawn_answer_worker(payload)

    def _spawn_answer_worker(self, payload: str) -> str | ProviderUnavailable:
        cmd = _worker_command()

        def _preexec() -> None:  # pragma: no cover - child-side, POSIX only
            try:
                os.nice(_NICE_INCREMENT)
            except OSError:
                pass

        try:
            proc = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_worker_timeout_s(),
                env=_allowlist_env(),
                preexec_fn=_preexec if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired:
            log.warning("Downloaded-model answer worker timed out; unavailable")
            return PROVIDER_UNAVAILABLE
        except OSError:
            log.warning("Downloaded-model answer worker could not be spawned; unavailable")
            return PROVIDER_UNAVAILABLE

        if proc.returncode != 0:
            log.warning("Downloaded-model answer worker exited %d; unavailable",
                        proc.returncode)
            return PROVIDER_UNAVAILABLE
        if len(proc.stdout or "") > _MAX_STDOUT_BYTES:
            log.warning("Downloaded-model answer output exceeds the stdout cap; unavailable")
            return PROVIDER_UNAVAILABLE
        return self._parse_text_envelope(proc.stdout)

    @staticmethod
    def _parse_text_envelope(stdout: str) -> str | ProviderUnavailable:
        """Decode the worker's ``{status, result}`` envelope into the raw answer string.

        Returns the ``result`` **string** on ``status == "ok"``, or
        :data:`PROVIDER_UNAVAILABLE` on ``unavailable`` / non-string result /
        any unparseable output.
        """
        text = (stdout or "").strip()
        if not text:
            return PROVIDER_UNAVAILABLE
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return PROVIDER_UNAVAILABLE
        if not isinstance(envelope, dict):
            return PROVIDER_UNAVAILABLE
        if envelope.get("status") == "ok":
            result = envelope.get("result")
            return result if isinstance(result, str) else PROVIDER_UNAVAILABLE
        return PROVIDER_UNAVAILABLE
