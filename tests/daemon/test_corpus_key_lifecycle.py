"""Corpus key staging lifecycle (search U8 — review fixes #3/#8).

The daemon stages the corpus key to a per-recording 0600 file for the engine
subprocess (different Keychain ACL identity), then MUST clean it up on teardown
and prune stale ones at daemon startup — the corpus key is Keychain-entitlement
protected, so a lingering plaintext 0600 copy would let any same-EUID process
decrypt the corpus. Staging fail-closed (returns None) is what lets ``spawn`` clamp
stills off rather than capture plaintext. Vision-free; key via the env-file channel.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from screencap import corpus_crypto
from screencap.capture_gate import CaptureGateResult
from screencap.daemon.event_bus import EventBus
from screencap.daemon.schema import RecordingStartRequest
from screencap.daemon.supervisor import Supervisor

pytestmark = pytest.mark.privacy


def _sup() -> Supervisor:
    return Supervisor(EventBus(), reconcile_on_init=False)


def _req() -> RecordingStartRequest:
    return RecordingStartRequest(name="demo", output_dir="/tmp/demo")


def _stage_key_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    key = os.urandom(32)
    src = tmp_path / "corpus.key"
    src.write_text(base64.b64encode(key).decode())
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(src))
    monkeypatch.setattr("screencap.config.get_base_dir", lambda: tmp_path)
    return key


def _gate(*, encrypted: bool) -> CaptureGateResult:
    return CaptureGateResult(
        capture_images=True, capture_images_encrypted=encrypted, reason="default_on"
    )


@pytest.mark.asyncio
async def test_stage_is_noop_when_not_encrypted(tmp_path, monkeypatch):
    _stage_key_via_env(tmp_path, monkeypatch)
    sup = _sup()
    assert await sup._stage_corpus_key(_req(), tmp_path / "demo", gate=_gate(encrypted=False)) is None
    assert sup._corpus_key_file is None


@pytest.mark.asyncio
async def test_stage_fails_closed_without_key(tmp_path, monkeypatch):
    # Encryption required but no key present → None; spawn keys off this to clamp
    # stills OFF (never plaintext). Nothing is staged.
    monkeypatch.setenv(corpus_crypto.CORPUS_KEY_FILE_ENV, str(tmp_path / "absent.key"))
    monkeypatch.setattr("screencap.config.get_base_dir", lambda: tmp_path)
    sup = _sup()
    assert await sup._stage_corpus_key(_req(), tmp_path / "demo", gate=_gate(encrypted=True)) is None
    assert sup._corpus_key_file is None


@pytest.mark.asyncio
async def test_stage_tracks_then_cleanup_removes_the_key_file(tmp_path, monkeypatch):
    _stage_key_via_env(tmp_path, monkeypatch)
    sup = _sup()
    capture_dir = tmp_path / "recordings" / "demo"

    env = await sup._stage_corpus_key(_req(), capture_dir, gate=_gate(encrypted=True))
    assert env is not None
    assert env["RECORD_IMAGES_ENCRYPTED"] == "1"
    staged = Path(env[corpus_crypto.CORPUS_KEY_FILE_ENV])
    assert staged.exists()
    assert staged.name == "corpus-key-demo.key"  # per-recording, not a shared corpus.key
    assert (staged.stat().st_mode & 0o777) == 0o600
    assert sup._corpus_key_file == staged

    sup._cleanup_corpus_key_file()  # teardown (via _reset_state in production)
    assert not staged.exists()
    assert sup._corpus_key_file is None


def test_prune_removes_stale_corpus_key_files(tmp_path, monkeypatch):
    monkeypatch.setattr("screencap.config.get_base_dir", lambda: tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    stray = run / "corpus-key-crashed.key"
    stray.write_text("leftover-after-SIGKILL")
    Supervisor._prune_stale_corpus_key_files()
    assert not stray.exists()
