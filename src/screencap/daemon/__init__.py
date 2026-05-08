"""Daemon public entry points."""

# ruff: noqa: I001
# Load-bearing import order: ``screencap._startup`` must run before any
# multiprocessing primitive is imported by daemon callers or spawned workers.

from __future__ import annotations

from screencap import _startup  # noqa: F401

import importlib
from typing import Any

__all__ = ["serve"]


def __getattr__(name: str) -> Any:
    if name == "serve":
        return importlib.import_module("screencap.daemon.server").serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
