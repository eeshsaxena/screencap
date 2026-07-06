"""LLM response schema for session→named-task segmentation.

Lifted verbatim from the Cloud Run processor (``scripts/process-recording/main.py``)
so both the cloud processor and the local pipeline drive one source of truth.
This module is deliberately cloud-free — no ``google.cloud`` / ``genai`` imports —
so it stays importable inside the daemon.
"""

from __future__ import annotations

# JSON schema for the LLM's structured segmentation output. Passed to the
# provider (Gemini's ``response_schema``, Foundation Models guided generation).
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["development", "communication", "research",
                                 "admin", "creative", "other"],
                    },
                    "apps_used": {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": ["start_time", "end_time", "name", "description",
                             "category", "apps_used", "confidence"],
            },
        },
        "summary": {
            "type": "object",
            "properties": {
                "overview": {"type": "string"},
                "primary_focus": {"type": "string"},
                "time_breakdown": {"type": "object"},
                "key_accomplishments": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            "required": ["overview", "primary_focus", "time_breakdown",
                         "key_accomplishments"],
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["tasks", "summary", "tags"],
}
