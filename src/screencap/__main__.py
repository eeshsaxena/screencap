"""`python -m screencap` entry point used by daemon engine subprocesses."""

from __future__ import annotations

from screencap.cli import cli


if __name__ == "__main__":
    cli()
