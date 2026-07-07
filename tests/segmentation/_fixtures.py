"""Shared in-memory fixture + source for segmentation tests.

The fixture data mirrors the golden generator (scratchpad) and
``tests/test_llm_segmentation.py``, so the characterization assertion in
``test_activity_summary.py`` — extracted package output == pre-refactor cloud
output — is meaningful. NOT GCS, NOT a live LLM: the deterministic
summary-building + validation are what's characterized.
"""

from __future__ import annotations

from screencap.segmentation.activity_summary import ActivitySource

MANIFESTS: list[dict] = [
    {"format_version": 2, "chunk_index": 0, "chunk_start": 1000.0,
     "chunk_end": 4600.0, "stats": {"total_events": 200, "total_window_switches": 5},
     "blocked_intervals": []},
    {"format_version": 2, "chunk_index": 1, "chunk_start": 4600.0,
     "chunk_end": 8200.0, "stats": {"total_events": 150, "total_window_switches": 3},
     "blocked_intervals": []},
]

EVENTS_0: list[dict] = [
    {"_meta": True, "format_version": 2},
    {"type": "window.switch", "timestamp": 1100.0,
     "app_bundle_id": "com.microsoft.VSCode", "window_title": "main.py — screencap"},
    {"type": "key.type", "timestamp": 1110.0, "text": "def foo():"},
    {"type": "key.shortcut", "timestamp": 1120.0, "text": "Cmd+S"},
    {"type": "mouse.singleclick", "timestamp": 1130.0},
    {"type": "mouse.scroll", "timestamp": 1140.0},
    {"type": "window.switch", "timestamp": 2000.0,
     "app_bundle_id": "com.google.Chrome", "window_title": "Gmail",
     "domain": "mail.google.com"},
    {"type": "key.type", "timestamp": 2010.0, "text": "Hey team"},
    {"type": "window.switch", "timestamp": 3000.0,
     "app_bundle_id": "com.microsoft.VSCode", "window_title": "auth.py — screencap"},
    {"type": "key.type", "timestamp": 3010.0, "text": "class Auth:"},
]

EVENTS_1: list[dict] = [
    {"_meta": True, "format_version": 2},
    {"type": "window.switch", "timestamp": 4700.0,
     "app_bundle_id": "com.microsoft.VSCode", "window_title": "auth.py — screencap"},
    {"type": "key.type", "timestamp": 4710.0, "text": "def token():"},
    {"type": "mouse.singleclick", "timestamp": 4720.0},
    {"type": "window.switch", "timestamp": 6000.0,
     "app_bundle_id": "com.tinyspeck.slackmacgap", "window_title": "#dev — Slack"},
    {"type": "key.type", "timestamp": 6010.0, "text": "PR is ready"},
]

TRANSCRIPT_0: dict = {
    "segments": [
        {"start": 50.0, "end": 55.0, "text": "Let me fix this bug first"},
        {"start": 950.0, "end": 955.0, "text": "Now let me check email"},
    ]
}

# LLM output fixture (as if a model returned it) — deterministic, no API call.
# Exercises: time_map hits, _parse_relative_time fallback (end "1:20:00" absent
# from time_map → session_start + 4800 = 5800), category pass-through, tag
# normalization (dedup "python", lowercase "Auth"→"auth", reject "BAD TAG!!").
LLM_RESULT: dict = {
    "tasks": [
        {"start_time": "0:00:00", "end_time": "0:15:00",
         "name": "Implement auth module", "description": "Wrote auth.py and tests.",
         "category": "development", "apps_used": ["VS Code"], "confidence": "high"},
        {"start_time": "0:16:40", "end_time": "1:20:00",
         "name": "Coordinate PR review", "description": "Pinged team on Slack about the PR.",
         "category": "communication", "apps_used": ["Slack"], "confidence": "medium"},
    ],
    "summary": {
        "overview": "Built the auth module then coordinated review.",
        "primary_focus": "development",
        "time_breakdown": {"development": 60, "communication": 40},
        "key_accomplishments": ["Shipped auth module", "Opened PR"],
    },
    "tags": ["python", "Auth", "code-review", "BAD TAG!!", "python"],
}


class InMemorySource:
    """An ``ActivitySource`` backed by in-memory event/transcript dicts.

    Feeds the builder the same data GCS would, without touching storage — the
    reader-parameterization is exactly what makes the extraction testable.
    Skips ``_meta`` rows to match the cloud ``_iterate_events`` contract.
    """

    def __init__(
        self,
        events_by_chunk: dict[int, list[dict]],
        transcripts_by_chunk: dict[int, dict] | None = None,
    ) -> None:
        self._events_by_chunk = events_by_chunk
        self._transcripts_by_chunk = transcripts_by_chunk or {}

    def iter_events(self):
        for chunk_idx in sorted(self._events_by_chunk):
            for evt in self._events_by_chunk[chunk_idx]:
                if not evt.get("_meta"):
                    yield evt

    def read_transcript(self, chunk_index: int) -> dict | None:
        return self._transcripts_by_chunk.get(chunk_index)


# Enforce the Protocol at import time (catches signature drift).
_source: ActivitySource = InMemorySource({})


def default_source() -> InMemorySource:
    """The canonical fixture source used by the characterization test."""
    return InMemorySource(
        events_by_chunk={0: EVENTS_0, 1: EVENTS_1},
        transcripts_by_chunk={0: TRANSCRIPT_0},
    )
