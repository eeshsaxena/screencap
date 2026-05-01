"""Tests for the macOS Privacy & Security deep-link helper.

The SwiftUI shell follows the same Apple URL contract independently in
``PermissionController.openSystemSettings``; the two implementations are
not coupled by tooling, just by the shared spec.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from screencap.recorder import _open_privacy_settings


def test_uses_macos_13_extension_url_form() -> None:
    """The helper must use the .extension URL form (macOS 13+).

    The legacy ``com.apple.preference.security`` form lands on a generic page
    on macOS 26+; the .extension form deep-links to the requested sub-pane.
    """
    with patch("screencap.recorder.subprocess.run") as run:
        _open_privacy_settings("Privacy_ScreenCapture")

    cmd = run.call_args.args[0]
    assert cmd[0] == "open"
    assert cmd[1] == (
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"
        "?Privacy_ScreenCapture"
    )


@pytest.mark.parametrize(
    "pane",
    ["Privacy_ScreenCapture", "Privacy_Accessibility", "Privacy_ListenEvent"],
)
def test_each_supported_pane_round_trips(pane: str) -> None:
    with patch("screencap.recorder.subprocess.run") as run:
        _open_privacy_settings(pane)
    url = run.call_args.args[0][1]
    assert url.endswith(f"?{pane}")
    assert ".extension?" in url


def test_passes_check_false() -> None:
    """The helper is fire-and-forget — `check=False` keeps it from raising
    on a non-zero `open` exit. A regression to `check=True` would let
    `CalledProcessError` propagate up the stack and break callers.
    """
    with patch("screencap.recorder.subprocess.run") as run:
        _open_privacy_settings("Privacy_ScreenCapture")
    assert run.call_args.kwargs.get("check") is False


def test_does_not_raise_on_nonzero_exit() -> None:
    """`check=False` means a non-zero `open` exit does not raise. If that
    contract regresses (someone flips to `check=True`), this test catches it."""
    with patch("screencap.recorder.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=["open"], returncode=1)
        # Must not raise.
        _open_privacy_settings("Privacy_ScreenCapture")
