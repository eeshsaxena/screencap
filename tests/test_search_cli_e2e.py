"""Real-CLI end-to-end of the search-by-default flip + decline (search U8, DoD).

Spawns the actual ``screencap`` CLI in a fully isolated ``HOME`` (so the module-level
``~/.screencap`` path constants resolve into a tmp dir at import — nothing touches the
developer's real corpus) and asserts the two Definition-of-Done behaviors against the
real binary:

  * ``search enable`` migrates an existing plaintext corpus to encrypted **with zero
    plaintext stills remaining**, rekeys the index, and flips the gate ON (search
    default-on) — only after the acknowledgment ``search enable`` records.
  * declining (``settings --set content_index_consent_declined=true``) durably holds
    the gate OFF.

All corpus/index/config I/O runs in subprocesses under the isolated HOME. Requires the
SQLCipher binding (dev machine); skipped where absent, matching the plan's framing.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("pysqlcipher3")

pytestmark = pytest.mark.privacy

_REPO = Path(__file__).resolve().parent.parent


def _cli(home: Path, *args: str, key_file: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCREENCAP_")}
    # Pin the vault container off: this suite exercises corpus/search encryption
    # (the plaintext content_index layout), not the on-disk container which now
    # defaults on (SCR-258).
    env.update(HOME=str(home), PYTHONPATH="src", SCREENCAP_CONTAINER_ENABLED="0")
    if key_file is not None:
        env["SCREENCAP_CORPUS_KEY_FILE"] = str(key_file)
    return subprocess.run(
        [sys.executable, "-m", "screencap", *args],
        env=env, cwd=str(_REPO), capture_output=True, text=True,
    )


def _py(home: Path, code: str, key_file: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCREENCAP_")}
    # Pin the vault container off: this suite exercises corpus/search encryption
    # (the plaintext content_index layout), not the on-disk container which now
    # defaults on (SCR-258).
    env.update(HOME=str(home), PYTHONPATH="src", SCREENCAP_CONTAINER_ENABLED="0")
    if key_file is not None:
        env["SCREENCAP_CORPUS_KEY_FILE"] = str(key_file)
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(_REPO), capture_output=True, text=True)


def _failure_event(stream: str) -> dict | None:
    """The `search_enable_failed` structured event in a captured stream, if present.

    Mirrors the Swift `SearchEnableEventLine.failure(inStderr:)` scan, so both sides of
    the contract are pinned by a test that reads the stream the same way."""
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "search_enable_failed":
            return event
    return None


def _status(home: Path, key_file: Path | None = None) -> dict:
    r = _cli(home, "search", "status", "--json", key_file=key_file)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_search_enable_flip_migrates_and_flips_gate(tmp_path):
    home = tmp_path / "home"
    ss = home / ".screencap" / "recordings" / "rec-1" / "screenshots"
    ss.mkdir(parents=True)
    key_file = tmp_path / "corpus.key"

    # Seed a plaintext corpus: one real JPEG still + a plaintext content-index row.
    jpeg = io.BytesIO()
    Image.new("RGB", (64, 32), "white").save(jpeg, format="JPEG")
    (ss / "100.000000.jpg").write_bytes(jpeg.getvalue())
    # Age it past the flip's active-writer grace (corpus_migrate._ACTIVE_WRITE_GRACE_S,
    # 2s) so this pre-existing still migrates now instead of being deferred as a
    # recorder's in-flight write — otherwise the test races that window on fast hosts.
    old = time.time() - 10
    os.utime(ss / "100.000000.jpg", (old, old))
    seed = _py(
        home,
        "from screencap.content_index import ContentIndex, IndexFrame, default_index_path;"
        "ix=ContentIndex(default_index_path());"
        "ix.write_frames('rec-1',[IndexFrame(timestamp_ms=100000,text='quarterlyreport findme')]);"
        "ix.close();"
        "print(open(str(default_index_path()),'rb').read(16)==b'SQLite format 3\\x00')",
        key_file=key_file,
    )
    assert seed.returncode == 0, seed.stderr
    assert seed.stdout.strip() == "True"  # seed index is plaintext

    before = _status(home, key_file)
    assert before["corpus_encrypted"] is False

    r = _cli(home, "search", "enable", key_file=key_file)
    assert r.returncode == 0, r.stderr

    # Zero plaintext stills remain (DoD); the index is now SQLCipher.
    assert sorted(p.name for p in ss.iterdir()) == ["100.000000.jpg.enc"]
    idx_header = (home / ".screencap" / "content_index.db").read_bytes()[:16]
    assert idx_header != b"SQLite format 3\x00"

    after = _status(home, key_file)
    assert after["corpus_encrypted"] is True
    assert after["disclosure_acknowledged"] is True
    assert after["content_index_enabled"] is True  # default-ON post-flip

    # The migration preserved the data: still decrypts to the original; row still found.
    verify = _py(
        home,
        "from screencap import corpus_crypto, still_io;"
        "from screencap.content_index import ContentIndex, default_index_path;"
        f"orig={base64.b64encode(jpeg.getvalue())!r};"
        "import base64;"
        f"dec=still_io.open_still(r'{ss / '100.000000.jpg.enc'}', corpus_crypto.load_corpus_key());"
        "ix=ContentIndex(default_index_path());"
        "hit=bool(ix.available and ix.search('quarterlyreport').hits); ix.close();"
        "print(dec==base64.b64decode(orig) and hit)",
        key_file=key_file,
    )
    assert verify.returncode == 0, verify.stderr
    assert verify.stdout.strip() == "True"


def test_new_install_disclosure_then_search_on_and_gate_healthy(tmp_path):
    """A fresh install: the disclosure would be shown (not acknowledged, not declined),
    the gate is OFF until acknowledgment, and after ``search enable`` search is ON and
    the readiness gate resolves default-on (DoD: 'new install lands with search ON')."""
    home = tmp_path / "home"
    (home / ".screencap").mkdir(parents=True)  # empty install — no recordings, no config
    key_file = tmp_path / "corpus.key"

    # Before acknowledgment: gate OFF, and the disclosure WOULD present.
    before = _status(home, key_file)
    assert before["corpus_encrypted"] is False
    assert before["content_index_enabled"] is False  # gate off until the disclosure is acknowledged
    # The disclosure WOULD present (Swift SearchDisclosurePolicy.shouldPresent mirror):
    # unacknowledged + not-declined ⇒ show.
    assert before["disclosure_acknowledged"] is False and before["consent_declined"] is False

    # Acknowledge via `search enable` (a no-op migration on an empty install) → search ON.
    r = _cli(home, "search", "enable", key_file=key_file)
    assert r.returncode == 0, r.stderr
    after = _status(home, key_file)
    assert after["corpus_encrypted"] is True
    assert after["disclosure_acknowledged"] is True
    assert after["content_index_enabled"] is True  # search ON

    # Gate healthy: the readiness gate resolves default-on for the enabled install.
    healthy = _py(
        home,
        "from screencap import capture_gate;"
        "g=capture_gate.gather_and_resolve(explicit_capture_images=None, scrub_enabled=True);"
        "print(g.capture_images and g.capture_images_encrypted and g.reason=='default_on')",
        key_file=key_file,
    )
    assert healthy.returncode == 0, healthy.stderr
    assert healthy.stdout.strip() == "True"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits these cases rely on")
def test_search_enable_key_failure_reports_the_reason_on_stderr(tmp_path):
    """A failed ``search enable`` puts its reason on STDERR, not stdout.

    The macOS disclosure sheet only ever sees ``CLIError.nonZeroExit(code:stderr:)``,
    which carries stderr — a reason printed to stdout is dropped and the user gets a
    bare "Couldn't turn on search. Please try again." Pins the reason code too, since
    the app maps it to actionable copy (a Keychain refusal is not retryable)."""
    home = tmp_path / "home"
    (home / ".screencap").mkdir(parents=True)
    # A directory that exists but refuses new files: `_write_key_file` mkdirs the
    # parent (fine) then O_CREATs the key (EACCES) → CorpusKeyUnavailable.
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        r = _cli(home, "search", "enable", key_file=locked / "corpus.key")
    finally:
        locked.chmod(0o700)

    assert r.returncode != 0, f"expected a non-zero exit, got 0\nstdout={r.stdout}"
    event = _failure_event(r.stderr)
    assert event["reason"] == "corpus_key_unavailable", event
    assert "corpus key" in event["detail"]  # the human-readable cause rides along
    assert _failure_event(r.stdout) is None  # not stdout-only (the original bug)

    # Pin the emitted SHAPE, not just the values: the Swift decoder
    # (`SearchEnableEventLine`) keys on exactly these names, and its fixture is a
    # captured paste that cannot notice a producer-side rename on its own.
    assert set(event) == {"type", "ts", "schema_version", "reason", "detail"}, event
    assert event["schema_version"] == 1, event

    assert _status(home)["corpus_encrypted"] is False  # gate stayed off


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits these cases rely on")
def test_search_enable_uncategorized_failure_still_reports_a_reason(tmp_path):
    """An unexpected exception reports ``unknown`` rather than escaping as a traceback.

    This is the catch-all arm — by definition the one that fires for failures nobody
    anticipated — so it is exactly where a silent regression would hurt most."""
    home = tmp_path / "home"
    recordings = home / ".screencap" / "recordings"
    recordings.mkdir(parents=True)
    key_file = tmp_path / "corpus.key"
    # Unreadable recordings dir: `migrate_corpus` calls `recordings_dir.iterdir()`
    # OUTSIDE its per-recording try/except, so the PermissionError propagates out of
    # `flip_corpus_to_encrypted` uncategorized.
    recordings.chmod(0o000)
    try:
        r = _cli(home, "search", "enable", key_file=key_file)
    finally:
        recordings.chmod(0o700)

    assert r.returncode != 0, f"expected a non-zero exit, got 0\nstdout={r.stdout}"
    event = _failure_event(r.stderr)
    assert event["reason"] == "unknown", event
    assert "PermissionError" in event["detail"], event  # detail names the exception type
    assert "Traceback" not in r.stderr  # handled, not an escaped crash


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits these cases rely on")
def test_search_enable_incomplete_migration_fails_instead_of_reporting_success(tmp_path):
    """An incomplete flip exits non-zero — it must never print "Search enabled."

    ``flip_corpus_to_encrypted`` *returns* (not raises) when stills survive as
    plaintext, leaving ``corpus_encrypted`` unset. Reporting success there tells the
    app search is on while the gate is off, and the disclosure re-appears next launch."""
    home = tmp_path / "home"
    ss = home / ".screencap" / "recordings" / "rec-1" / "screenshots"
    ss.mkdir(parents=True)
    key_file = tmp_path / "corpus.key"

    jpeg = io.BytesIO()
    Image.new("RGB", (64, 32), "white").save(jpeg, format="JPEG")
    still = ss / "100.000000.jpg"
    still.write_bytes(jpeg.getvalue())
    old = time.time() - 10  # past the active-writer grace, so it is not merely deferred
    os.utime(still, (old, old))
    still.chmod(0o000)  # unreadable → the encrypt step fails and the plaintext survives
    try:
        r = _cli(home, "search", "enable", key_file=key_file)
    finally:
        still.chmod(0o600)

    assert r.returncode != 0, f"expected a non-zero exit, got 0\nstdout={r.stdout}"
    assert _failure_event(r.stderr)["reason"] == "migration_incomplete", r.stderr
    assert "Search enabled" not in r.stdout, r.stdout

    assert _status(home, key_file)["corpus_encrypted"] is False


def test_decline_durably_holds_gate_off(tmp_path):
    home = tmp_path / "home"
    (home / ".screencap").mkdir(parents=True)

    r = _cli(home, "settings", "--set", "content_index_consent_declined=true")
    assert r.returncode == 0, r.stderr

    st = _status(home)
    assert st["consent_declined"] is True
    assert st["content_index_enabled"] is False

    # Durable: a second read (fresh process) still shows declined + OFF.
    st2 = _status(home)
    assert st2["consent_declined"] is True and st2["content_index_enabled"] is False
