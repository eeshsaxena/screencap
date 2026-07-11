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
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("pysqlcipher3")

pytestmark = pytest.mark.privacy

_REPO = Path(__file__).resolve().parent.parent


def _cli(home: Path, *args: str, key_file: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCREENCAP_")}
    env.update(HOME=str(home), PYTHONPATH="src")
    if key_file is not None:
        env["SCREENCAP_CORPUS_KEY_FILE"] = str(key_file)
    return subprocess.run(
        [sys.executable, "-m", "screencap", *args],
        env=env, cwd=str(_REPO), capture_output=True, text=True,
    )


def _py(home: Path, code: str, key_file: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCREENCAP_")}
    env.update(HOME=str(home), PYTHONPATH="src")
    if key_file is not None:
        env["SCREENCAP_CORPUS_KEY_FILE"] = str(key_file)
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=str(_REPO), capture_output=True, text=True)


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
