"""Daemon-observable inputs to the intelligence "usable" verdict (SCR — honest status, U1).

The app composes the final live "usable" verdict from two sources: its own fresh
``OnDeviceModelStatus.probe()`` (Apple-Intelligence availability — a Swift-only
fact) and the daemon-observable config facts assembled here. The daemon deliberately
does **not** probe Apple-Intelligence availability: ``SystemLanguageModel.availability``
is Swift-only, so a daemon-side probe would either only confirm the helper binary is
resolvable (which would say "usable" even when Apple Intelligence is toggled off — the
exact "says Ready, got nothing" bug this work removes) or spawn a full model run on the
record/settings hot path. Instead the daemon reports only what it can observe directly.

This module is import-light and cloud-free: it reads config getters and the
downloaded-model install marker, and computes the macOS floor from the Python
platform version. It never spawns the helper.
"""

from __future__ import annotations

import platform
from typing import Any

# Apple Foundation Models (the on-device path) require macOS 26+. Below the floor
# the on-device backend can never run, so the app must resolve the verdict to
# cloud/BYO-only regardless of any availability probe.
_MACOS_ONDEVICE_FLOOR = 26


def _macos_floor_ok() -> bool:
    """True when the host is macOS >= the on-device floor.

    Non-macOS hosts (and macOS below the floor) return False — the on-device path
    is not eligible there. Mirrors the Swift ``OnDeviceModelStatus.osUnsupported``
    check on the Python side, without importing anything macOS-specific.
    """
    if platform.system() != "Darwin":
        return False
    release = platform.mac_ver()[0]  # e.g. "26.1"; "" when unavailable
    if not release:
        return False
    try:
        major = int(release.split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    return major >= _MACOS_ONDEVICE_FLOOR


def intelligence_verdict_inputs() -> dict[str, Any]:
    """Assemble the daemon-observable inputs to the "usable" verdict.

    Returns the facts the daemon can observe directly, for the app to combine with
    its fresh Apple-Intelligence availability probe:

    - ``active_provider`` — the configured active provider (default ``"on-device"``).
    - ``downloaded_model_installed`` — whether the downloadable on-device model is
      installed (the ``.installed`` marker is present).
    - ``cloud_provider`` — the configured cloud fallback provider id, or ``None``.
    - ``cloud_summary_consent`` — whether a consented cloud summary/title fallback is
      permitted.
    - ``os_floor_ok`` — whether the host meets the macOS on-device floor.

    Never probes Apple-Intelligence availability and never spawns the helper.
    """
    from screencap import config
    from screencap.models import DEFAULT_MODEL_ID
    from screencap.models.download import is_model_installed
    from screencap.segmentation.local_model.runtime import select_runtime

    runtime = select_runtime()
    return {
        "active_provider": config.get_llm_provider(),
        "downloaded_model_installed": bool(is_model_installed(DEFAULT_MODEL_ID, runtime)),
        "cloud_provider": config.get_llm_cloud_provider(),
        "cloud_summary_consent": config.get_summary_cloud_consent(),
        "os_floor_ok": _macos_floor_ok(),
    }
