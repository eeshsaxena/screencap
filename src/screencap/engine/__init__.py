"""Screencap Engine - GUI interaction capture.

Platform-agnostic event streams with time-aligned media.

This package exposes a deliberately small public surface so that
``import screencap.engine`` stays cheap (no pynput, av, mss, matplotlib,
etc.). Reach into the submodules directly for everything else
(``screencap.engine.recorder``, ``screencap.engine.events``,
``screencap.engine.screen_recorder`` for the seam types, ...).
"""

from __future__ import annotations

__version__ = "0.1.0"

from screencap.engine.capture import Capture, CaptureSession
from screencap.engine.visualize import create_html

__all__ = [
    "Capture",
    "CaptureSession",
    "__version__",
    "create_html",
]
