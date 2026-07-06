"""Session→named-task segmentation core (importable, source-agnostic).

Extracted from the Cloud Run processor (``scripts/process-recording/main.py``)
so both the cloud processor and the local pipeline drive one code path. The
package is deliberately **cloud-free** — no ``google.cloud`` / ``storage`` /
``genai`` imports — so it stays importable inside the daemon.

Submodules:

- ``activity_summary`` — ``build_activity_summary(recording_name, manifests,
  source)`` builds the compact text activity summary fed to the LLM. The
  event/transcript *source* is injected (``ActivitySource``) rather than
  hard-wired to GCS; the cloud processor passes a GCS-backed source, a local
  caller passes a filesystem / in-memory one.
- ``schema`` — ``_RESPONSE_SCHEMA``, the JSON schema for the LLM's structured
  task output.
- ``validate`` — ``validate_llm_tasks(...)`` converts relative→Unix
  timestamps, rejects overlapping / zero-duration tasks, and repairs
  recoverable schema drift.

Import submodules directly.
"""
