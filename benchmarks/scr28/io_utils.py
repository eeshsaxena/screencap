"""Pure-stdlib I/O helpers shared by both runners and the scorer.

No ML or ``screencap`` imports — must load in the repo env *and* the isolated
privacy-filter venv.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_input_texts(path: Path | str) -> list[tuple[str, str]]:
    """Read ``(case_id, text)`` pairs from a JSONL file.

    Accepts both ``tier2_testbed/inputs.jsonl`` (``{"case_id"|"id", "text",
    "modality"}``) and ``gold.jsonl`` (same plus ``expected``) — only the id and
    text are read, so a runner can score against either without the gold labels.
    """
    pairs: list[tuple[str, str]] = []
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            case_id = d.get("case_id") or d.get("id")
            if case_id is None:
                raise ValueError(f"input record missing 'id'/'case_id': {line[:80]!r}")
            pairs.append((str(case_id), str(d["text"])))
    return pairs
