"""Filesystem-backed ``ActivitySource`` for on-Mac (local) segmentation (U4).

The cloud processor passes a GCS-backed ``ActivitySource`` to
``build_activity_summary``; the local pipeline (``terminal_stage``) passes THIS
one, which reads the same per-chunk artifacts the recorder wrote to disk:

  - ``chunk_NNNN_manifest.json`` — the v2 chunk metadata (chunk_index /
    chunk_start / chunk_end); enumerated by :func:`load_local_manifests`.
  - ``events_NNNN.jsonl``        — the per-chunk event stream (``_meta`` rows
    excluded, matching the cloud ``_iterate_events`` contract).
  - ``transcript_NNNN.json``     — the per-chunk transcript (``{"segments": …}``).

The module is deliberately light — no cloud/vendor imports — mirroring the rest
of the ``segmentation`` package. All reads are best-effort: a missing or
unparseable file is skipped rather than raised, so a partially-recovered
recording still yields whatever activity it has (the summary builder returns
``None`` when there is nothing to segment).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


def load_local_manifests(recording_dir: Path | str) -> list[dict]:
    """Return the parsed ``chunk_NNNN_manifest.json`` dicts, in chunk order.

    Only manifests carrying the fields the summary builder needs
    (``chunk_index`` / ``chunk_start`` / ``chunk_end``) are returned — a
    manifest missing any of them (a torn write, or a legacy shape) is skipped
    so it cannot break the ``min``/``max`` over the window. Best-effort per
    file: a JSON decode error is logged and skipped, never raised.
    """
    recording_dir = Path(recording_dir)
    manifests: list[dict] = []
    for p in sorted(recording_dir.glob("chunk_*_manifest.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("local_source: unreadable manifest %s; skipping", p.name)
            continue
        if not isinstance(data, dict):
            continue
        if all(k in data for k in ("chunk_index", "chunk_start", "chunk_end")):
            manifests.append(data)
    manifests.sort(key=lambda m: m["chunk_index"])
    return manifests


class LocalActivitySource:
    """An ``ActivitySource`` reading a recording's on-disk chunk artifacts.

    Constructed with the recording dir and its manifests (from
    :func:`load_local_manifests`) so ``iter_events`` walks chunks in the same
    order the cloud path does.
    """

    def __init__(self, recording_dir: Path | str, manifests: list[dict]) -> None:
        self._recording_dir = Path(recording_dir)
        self._manifests = manifests

    def iter_events(self) -> Iterable[dict]:
        """Yield parsed events from each chunk's ``events_NNNN.jsonl``, in order.

        ``_meta`` rows are excluded (the cloud ``_iterate_events`` contract).
        A missing file, unreadable line, or JSON error is skipped, never fatal.
        """
        for manifest in sorted(self._manifests, key=lambda m: m["chunk_index"]):
            idx = manifest["chunk_index"]
            path = self._recording_dir / f"events_{idx:04d}.jsonl"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(evt, dict) and not evt.get("_meta"):
                    yield evt

    def read_transcript(self, chunk_index: int) -> dict | None:
        """Return the parsed ``transcript_NNNN.json`` for a chunk, or ``None``.

        Owns decode/JSON-parse error handling and returns ``None`` on any
        failure, matching the cloud processor's skip-on-error behavior.
        """
        path = self._recording_dir / f"transcript_{chunk_index:04d}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None
