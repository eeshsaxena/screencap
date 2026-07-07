"""Model download + integrity engine (U4, KTD5, SCR-239).

The real Hugging Face fetch cannot run in CI; these inject a fake ``snapshot_fn``
that materializes fixture files, and exercise the fail-closed verify path:
sha256 mismatch, disallowed format, missing file, symlink, interruption, and
insufficient disk all leave the model NOT installed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import types

import pytest

from screencap.models import download as dl
from screencap.models import registry
from screencap.models.download import (
    ModelNotPinnedError,
    download_model,
    get_disclosed_size,
    get_installed_model_path,
)
from screencap.models.registry import FileSpec, ModelSpec, Variant

_RUNTIME = "llamacpp"
_TEST_ID = "test-model"
_WEIGHTS = b"fake gguf weights" * 64


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def test_model(monkeypatch):
    """Register a pinned test model whose single file hashes to a known sha256."""
    fspec = FileSpec(name="model.gguf", sha256=_sha(_WEIGHTS), size_bytes=len(_WEIGHTS))
    spec = ModelSpec(
        id=_TEST_ID,
        display_name="Test",
        license="Apache-2.0",
        variants={_RUNTIME: Variant(
            repo="acme/test", revision="deadbeef" * 5, allowed_format="gguf",
            files=(fspec,),
        )},
    )
    monkeypatch.setitem(registry.MODELS, _TEST_ID, spec)
    return spec


def _good_snapshot(repo, revision, local_dir, filenames):
    (local_dir / "model.gguf").write_bytes(_WEIGHTS)


def _dl(tmp_path, **kw):
    return download_model(_TEST_ID, runtime=_RUNTIME, models_dir=tmp_path, **kw)


class TestHappyPath:
    def test_verifies_and_installs(self, tmp_path, test_model):
        result = _dl(tmp_path, snapshot_fn=_good_snapshot)
        assert result.state == "installed"
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) == result.path
        assert (result.path / "model.gguf").read_bytes() == _WEIGHTS
        assert (result.path / ".installed").is_file()

    def test_disclosed_size_matches_manifest(self, test_model):
        assert get_disclosed_size(_TEST_ID, _RUNTIME) == len(_WEIGHTS)

    def test_reinstall_is_idempotent(self, tmp_path, test_model):
        first = _dl(tmp_path, snapshot_fn=_good_snapshot)
        assert first.state == "installed"

        def _boom(*a):  # must not be called on the idempotent path
            raise AssertionError("snapshot should not run for an installed model")

        second = _dl(tmp_path, snapshot_fn=_boom)
        assert second.state == "installed" and second.already_installed

    @pytest.mark.skipif(os.name != "posix", reason="perms are POSIX-only")
    def test_installed_files_are_0600(self, tmp_path, test_model):
        result = _dl(tmp_path, snapshot_fn=_good_snapshot)
        mode = stat.S_IMODE((result.path / "model.gguf").stat().st_mode)
        assert mode == 0o600
        assert stat.S_IMODE(result.path.stat().st_mode) == 0o700


@pytest.mark.privacy
class TestFailClosed:
    def test_sha256_mismatch_does_not_install(self, tmp_path, test_model):
        def bad(repo, rev, local_dir, files):
            (local_dir / "model.gguf").write_bytes(b"tampered weights")

        result = _dl(tmp_path, snapshot_fn=bad)
        assert result.state == "failed" and "sha256-mismatch" in result.reason
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    def test_disallowed_format_refused(self, tmp_path, test_model):
        def evil(repo, rev, local_dir, files):
            (local_dir / "model.gguf").write_bytes(_WEIGHTS)
            (local_dir / "loader.py").write_text("import os")  # executable-on-load

        result = _dl(tmp_path, snapshot_fn=evil)
        assert result.state == "failed" and "disallowed-format" in result.reason
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    def test_missing_file_refused(self, tmp_path, test_model):
        result = _dl(tmp_path, snapshot_fn=lambda *a: None)  # writes nothing
        assert result.state == "failed" and "missing-file" in result.reason
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    @pytest.mark.skipif(os.name != "posix", reason="symlink test is POSIX-only")
    def test_symlink_refused(self, tmp_path, test_model):
        elsewhere = tmp_path / "elsewhere.gguf"
        elsewhere.write_bytes(_WEIGHTS)

        def linky(repo, rev, local_dir, files):
            os.symlink(elsewhere, local_dir / "model.gguf")

        result = _dl(tmp_path, snapshot_fn=linky)
        assert result.state == "failed" and "symlink-refused" in result.reason

    def test_interrupted_download_cancels_clean(self, tmp_path, test_model):
        stop = threading.Event()

        def interrupt(repo, rev, local_dir, files):
            (local_dir / "model.gguf").write_bytes(_WEIGHTS)
            stop.set()  # cancelled after bytes land, before install

        result = _dl(tmp_path, snapshot_fn=interrupt, stop_event=stop)
        assert result.state == "cancelled"
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None
        # No half-populated dir left behind.
        assert not (tmp_path / _TEST_ID / (_RUNTIME + ".partial")).exists()

    def test_insufficient_disk_fails_before_fetch(self, tmp_path, test_model, monkeypatch):
        touched = {"snapshot": False}

        def snap(*a):
            touched["snapshot"] = True

        monkeypatch.setattr(
            dl.shutil, "disk_usage",
            lambda p: types.SimpleNamespace(total=0, used=0, free=1),
        )
        result = _dl(tmp_path, snapshot_fn=snap)
        assert result.state == "failed" and "insufficient-disk" in result.reason
        assert touched["snapshot"] is False  # never fetched


class TestPinningAndResolution:
    def test_unpinned_variant_raises(self, tmp_path):
        # The shipped default model is PLACEHOLDER-pinned until release QA.
        with pytest.raises(ModelNotPinnedError):
            download_model(
                registry.DEFAULT_MODEL_ID, runtime="llamacpp", models_dir=tmp_path,
                snapshot_fn=_good_snapshot,
            )

    def test_not_installed_returns_none(self, tmp_path, test_model):
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    def test_unknown_model_fails(self, tmp_path):
        result = download_model("no-such-model", runtime="llamacpp", models_dir=tmp_path)
        assert result.state == "failed" and result.reason == "unknown-model"
