"""Tests for the macOS Privacy & Security deep-link helper.

Mirrors the SwiftUI ``PermissionController.openSystemSettings`` URL contract so
the CLI and the SwiftUI app land on the same panes on macOS 13+.
"""
from __future__ import annotations

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
