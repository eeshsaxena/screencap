"""E2EE ciphertext guard in the Cloud Run processor (SCR-238 KD5 hardening).

The ``process-recording`` service is plaintext-only. An end-to-end-encrypted
recording uploads ciphertext manifests by construction (plan KD5/R13), and this
service must no-op on them *explicitly* rather than relying on an emergent
``json.loads`` failure. These tests pin that guard and pin the local magic
constant against ``screencap.cloud_crypto`` (which the container does not
vendor, so the value is duplicated in ``main.py``).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.privacy

# Stub Cloud Run deps not installed in the dev venv (mirrors
# tests/test_llm_segmentation.py's shim) so ``import main`` works.
_ff = types.ModuleType("functions_framework")
_ff.cloud_event = lambda fn: fn  # no-op decorator
sys.modules.setdefault("functions_framework", _ff)

_gcs = types.ModuleType("google.cloud.storage")
_gcs.Client = MagicMock
_gcs.Bucket = MagicMock
sys.modules.setdefault("google.cloud.storage", _gcs)
sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
sys.modules.setdefault("google", types.ModuleType("google"))
sys.modules["google"].cloud = sys.modules["google.cloud"]
sys.modules["google.cloud"].storage = _gcs

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "process-recording"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import main  # noqa: E402

_MAGIC = b"SCRE2E"  # == screencap.cloud_crypto.MAGIC
_MANIFEST = "recordings/rec-x/chunk_0000_manifest.json"


def test_local_magic_matches_cloud_crypto():
    """The duplicated magic must never drift from the source of truth.

    ``main.py`` cannot import ``cloud_crypto`` (not vendored into the
    container), so it copies the constant. This is the cross-check.
    """
    from screencap import cloud_crypto

    assert main._E2EE_MAGIC == cloud_crypto.MAGIC


def test_detects_ciphertext_manifest():
    with patch.object(main, "_blob_bytes", return_value=_MAGIC + b"\x01\x00\x00\x40ciph"):
        assert main._manifests_are_encrypted([_MANIFEST]) is True


def test_plaintext_json_manifest_is_not_encrypted():
    with patch.object(main, "_blob_bytes", return_value=b'{"format_version": 2}'):
        assert main._manifests_are_encrypted([_MANIFEST]) is False


def test_empty_or_missing_manifest_is_not_encrypted():
    assert main._manifests_are_encrypted([]) is False
    with patch.object(main, "_blob_bytes", return_value=None):
        assert main._manifests_are_encrypted([_MANIFEST]) is False


def test_handler_skips_encrypted_recording_and_emits_no_timeline():
    """An encrypted recording writes ``skipped_encrypted`` and no session output."""
    rec = "rec-20260712T120000"
    sentinel_obj = f"recordings/{rec}/recording_complete.json"
    status_obj = f"sessions/{rec}/_processing_status.json"
    manifest_obj = f"recordings/{rec}/chunk_0000_manifest.json"

    def fake_blob_bytes(name):
        if name == sentinel_obj:
            return json.dumps(
                {"sentinel_id": "s1", "chunks_expected": 1, "show_on_website": True}
            ).encode()
        if name == status_obj:
            return None  # idempotency: not yet processed
        if name == manifest_obj:
            return _MAGIC + b"ciphertext-bytes"
        return None

    uploads: list[tuple[str, dict]] = []

    event = types.SimpleNamespace(data={"name": sentinel_obj})

    with patch.object(main, "_blob_bytes", side_effect=fake_blob_bytes), \
            patch.object(main, "_list_manifests", return_value=[manifest_obj]), \
            patch.object(main, "_upload_json", side_effect=lambda n, o: uploads.append((n, o))), \
            patch.object(main, "_upload_text"), \
            patch.object(main, "_bucket"):
        main.process_recording(event)

    status_writes = [obj for (name, obj) in uploads if name == status_obj]
    assert status_writes, "expected a _processing_status.json write"
    assert status_writes[-1]["status"] == "skipped_encrypted"
    # No enrichment output was produced for the ciphertext recording.
    assert not any(name.endswith("timeline.json") for (name, _) in uploads)
