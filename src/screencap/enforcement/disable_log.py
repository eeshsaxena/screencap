"""Atomic JSONL writer for the menubar disable audit log.

Each line records one disable event with row counts. The frontmatter line
records ``format_version`` and ``screencap_version`` so v1 readers can
detect schema migrations.

Lives at ``<capture_dir>/.menubar_disable_log.jsonl``. Append-only with
an in-process lock; not crash-durable per line because disable events
are infrequent and the recorder flushes on graceful shutdown.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from screencap import __version__

_LOG_FILENAME = ".menubar_disable_log.jsonl"
_FORMAT_VERSION = 1


class DisableLogWriter:
    """Append-only writer for the disable audit log."""

    def __init__(self, capture_dir: str | os.PathLike[str]) -> None:
        self._path = Path(capture_dir) / _LOG_FILENAME
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry: dict[str, Any]) -> None:
        """Atomically append one disable entry to the log."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            needs_meta = (
                not self._path.exists() or self._path.stat().st_size == 0
            )
            with open(self._path, "a", encoding="utf-8") as f:
                if needs_meta:
                    f.write(json.dumps({
                        "_meta": True,
                        "format_version": _FORMAT_VERSION,
                        "screencap_version": __version__,
                    }) + "\n")
                f.write(json.dumps(entry) + "\n")
                f.flush()
