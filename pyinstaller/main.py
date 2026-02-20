"""Frozen-binary entry point. Must call freeze_support() before any other imports."""

import multiprocessing
multiprocessing.freeze_support()

from screencap.cli import cli
cli()
