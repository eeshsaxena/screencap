"""macOS (Darwin) platform-specific implementations.

This module provides macOS-specific functionality for:
- Screen capture using Quartz
- Display information (resolution, Retina pixel ratio)
- Accessibility permission checking
"""

from __future__ import annotations

import sys

if sys.platform != "darwin":
    raise ImportError("This module is only available on macOS")


class DarwinPlatform:
    """macOS platform provider.

    Provides macOS-specific implementations for screen capture,
    display information, and accessibility checking.
    """

    @staticmethod
    def get_screen_dimensions() -> tuple[int, int]:
        """Get screen dimensions in physical pixels.

        Uses Quartz directly to avoid triggering the macOS Sequoia
        screen-capture consent dialog (which ImageGrab.grab() causes).

        Returns:
            Tuple of (width, height) in physical pixels.
        """
        try:
            import Quartz

            main_display = Quartz.CGMainDisplayID()
            width = Quartz.CGDisplayPixelsWide(main_display)
            height = Quartz.CGDisplayPixelsHigh(main_display)
            return (width, height)
        except Exception:
            return (1920, 1080)

    @staticmethod
    def get_display_pixel_ratio() -> float:
        """Get the display pixel ratio for Retina displays.

        Uses Quartz directly to avoid triggering the macOS Sequoia
        screen-capture consent dialog (which ImageGrab.grab() and
        mss.mss() cause via CGWindowListCreateImage).

        Returns 2.0 for Retina displays, 1.0 for standard displays.

        Returns:
            Pixel ratio (physical pixels / logical pixels).
        """
        try:
            import Quartz

            main_display = Quartz.CGMainDisplayID()
            physical_width = Quartz.CGDisplayPixelsWide(main_display)

            mode = Quartz.CGDisplayCopyDisplayMode(main_display)
            if mode:
                logical_width = Quartz.CGDisplayModeGetWidth(mode)
                if logical_width > 0:
                    return physical_width / logical_width

            return 1.0
        except Exception:
            return 1.0

    @staticmethod
    def is_screen_recording_enabled() -> bool:
        """Check if Screen Recording permission is granted.

        macOS requires Screen Recording permission for capturing screen content.
        Without it, mss.grab() returns all-black frames with no error.

        Returns:
            True if permission is granted, False otherwise.
        """
        try:
            import Quartz

            return Quartz.CGPreflightScreenCaptureAccess()
        except (ImportError, AttributeError):
            return True  # Assume enabled if we can't check

    @staticmethod
    def is_input_monitoring_enabled() -> bool:
        """Check if Input Monitoring permission is granted.

        Input Monitoring is required for listen-only CGEventTaps (used for
        trackpad gesture capture). This is a lighter permission than
        Accessibility.

        Returns:
            True if permission is granted, False otherwise.
        """
        try:
            import Quartz

            return Quartz.CGPreflightListenEventAccess()
        except (ImportError, AttributeError):
            return True  # Assume enabled if we can't check

    @staticmethod
    def is_accessibility_enabled() -> bool:
        """Check if accessibility permissions are enabled.

        macOS requires accessibility permissions for capturing
        keyboard and mouse events globally.

        Returns:
            True if accessibility is enabled, False otherwise.
        """
        try:
            import Quartz  # noqa: F401 - needed for ApplicationServices

            # Check if we can access accessibility features
            # This uses the AXIsProcessTrustedWithOptions function
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            # Check without prompting
            options = {kAXTrustedCheckOptionPrompt: False}
            return AXIsProcessTrustedWithOptions(options)
        except ImportError:
            # If ApplicationServices is not available, try a simpler check
            try:
                import subprocess

                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to get name of first process',
                    ],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return True  # Assume enabled if we can't check
        except Exception:
            return True  # Assume enabled if we can't check

    @staticmethod
    def get_active_window_info() -> dict | None:
        """Get information about the currently active window.

        Returns:
            Dictionary with window info (title, app_name, bounds) or None.
        """
        try:
            import Quartz

            # Get the list of windows
            options = Quartz.kCGWindowListOptionOnScreenOnly
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)

            if not window_list:
                return None

            # Find the frontmost window (layer 0 is typically the frontmost)
            for window in window_list:
                layer = window.get("kCGWindowLayer", -1)
                if layer == 0:
                    bounds = window.get("kCGWindowBounds", {})
                    return {
                        "title": window.get("kCGWindowName", ""),
                        "app_name": window.get("kCGWindowOwnerName", ""),
                        "bounds": {
                            "x": bounds.get("X", 0),
                            "y": bounds.get("Y", 0),
                            "width": bounds.get("Width", 0),
                            "height": bounds.get("Height", 0),
                        },
                        "pid": window.get("kCGWindowOwnerPID", 0),
                    }

            return None
        except Exception:
            return None


__all__ = ["DarwinPlatform"]
