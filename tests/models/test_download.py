"""Model download + integrity engine (U4, KTD5, SCR-239).

The real Hugging Face fetch cannot run in CI; these inject a fake ``snapshot_fn``
that materializes fixture files, and exercise the fail-closed verify path:
sha256 mismatch, disallowed format, missing file, symlink, interruption, stall,
cancellation, and insufficient disk all leave the model NOT installed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
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

    def test_reports_progress_during_the_fetch(self, tmp_path, test_model):
        """Progress must advance WHILE the snapshot runs, not only 0% → 100%.

        ``snapshot_download`` blocks for the whole multi-GB transfer and offers no
        byte-level callback, so without the staging sampler the engine reports
        ``(0, total)`` for minutes on end and every UI renders a frozen bar that
        is indistinguishable from a hang (the QA "stuck at 0%" report).
        """
        seen: list[tuple[int, int]] = []
        sampled = threading.Event()

        def chunked_snapshot(repo, revision, local_dir, filenames):
            half = len(_WEIGHTS) // 2
            (local_dir / "model.gguf").write_bytes(_WEIGHTS[:half])
            # Hold the partial file on disk until the sampler sees it (bounded, so
            # a non-sampling engine fails the assertion below rather than hanging).
            sampled.wait(5)
            (local_dir / "model.gguf").write_bytes(_WEIGHTS)

        def progress_cb(done, total):
            seen.append((done, total))
            if 0 < done < total:
                sampled.set()

        result = _dl(tmp_path, snapshot_fn=chunked_snapshot, progress_cb=progress_cb)

        assert result.state == "installed"
        assert any(0 < done < total for done, total in seen), (
            f"no intermediate progress reading; the bar would sit at 0%: {seen}"
        )
        # Never over-report: a reading above the disclosed total would drive a
        # >100% bar.
        assert all(done <= total for done, total in seen)

    def test_xet_pin_restores_only_when_the_last_fetch_exits(self):
        """The Xet switch is process-global, so its restore must be depth-counted.

        An abandoned (stalled) fetch thread can still be inside the pin when a
        retry enters it. A naive save/restore would let whichever exits first
        write the *pinned* value back as the original, leaving the daemon's other
        Hugging Face consumers permanently flipped.
        """
        from huggingface_hub import constants as hf_constants

        original = hf_constants.HF_HUB_DISABLE_XET
        try:
            hf_constants.HF_HUB_DISABLE_XET = False
            with dl._xet_disabled():
                with dl._xet_disabled():
                    assert hf_constants.HF_HUB_DISABLE_XET is True
                # Inner exit must NOT restore — a fetch is still in flight.
                assert hf_constants.HF_HUB_DISABLE_XET is True
            assert hf_constants.HF_HUB_DISABLE_XET is False
        finally:
            hf_constants.HF_HUB_DISABLE_XET = original

    # Marked so it runs in CI's `-m privacy` lane: it pins the fetch host, and an
    # unmarked guard for a CI-dark function would itself never run in CI.
    @pytest.mark.privacy
    def test_default_snapshot_pins_xet_off_and_the_host_for_the_fetch(self, tmp_path, monkeypatch):
        """The live fetch must apply both pins *at call time* (SCR-294).

        ``_default_snapshot`` is the one function no other test reaches — every
        other test injects ``snapshot_fn`` — so its two safety pins are otherwise
        CI-dark, and dropping either leaves the whole suite green:

        - **Xet off.** Both shipped variants are Xet-backed, and Xet assembles
          into ``local_dir`` only at the very end. Every bytes-on-disk reading
          (the progress bar *and* the stall clock) dies without this pin: the bar
          sits at 0% for the entire multi-GB fetch, and the stall guard starves
          and fails a healthy download.
        - **Host pinned.** ``HF_ENDPOINT`` is read once at import, so the host
          can only be pinned per call; the repo+commit+sha256 pin is incomplete
          without it.

        Asserting *inside* the stubbed call is the point — checking after it
        returns would pass even if the pin were applied around the wrong scope.
        """
        import huggingface_hub
        from huggingface_hub import constants as hf_constants

        seen: dict[str, object] = {}

        def fake_snapshot(**kwargs):
            seen["disable_xet"] = hf_constants.HF_HUB_DISABLE_XET
            seen["endpoint"] = kwargs.get("endpoint")

        monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
        monkeypatch.setattr(hf_constants, "HF_HUB_DISABLE_XET", False)

        dl._default_snapshot("acme/test", "deadbeef" * 5, tmp_path, ["model.gguf"])

        assert seen["disable_xet"] is True, (
            "Xet was live during the fetch; the staging dir stays flat and the "
            "progress bar sits at 0% for the whole download (SCR-294)"
        )
        assert seen["endpoint"] == dl._HF_ENDPOINT
        # The pin is scoped to the fetch, not leaked to the daemon's other consumers.
        assert hf_constants.HF_HUB_DISABLE_XET is False


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
        # No half-populated dir left behind. Globbed, not an exact path: staging is
        # uniquely named per attempt, so an exact-path check would pass vacuously.
        assert not list((tmp_path / _TEST_ID).glob(_RUNTIME + ".partial*"))

    def test_stalled_fetch_fails_instead_of_hanging(self, tmp_path, test_model, monkeypatch):
        """A fetch that stops producing bytes must fail, not block forever.

        ``snapshot_download`` has no overall deadline, and its internal retry
        budget resets on every chunk that arrives, so a wedged transfer blocks the
        engine indefinitely: the daemon job stays ``running``, the status verb
        keeps saying ``downloading``, and the idle-shutdown busy predicate pins the
        process. The bar simply stops moving — no error, no Retry, and (because the
        stop flag is only read after the fetch returns) no working Cancel either.
        """
        monkeypatch.setattr(dl, "_STALL_TIMEOUT_S", 0.4)
        release = threading.Event()
        attempts: list = []

        # No progress_cb is passed: the stall guard must not depend on one.
        def wedged(repo, rev, local_dir, files):
            attempts.append(local_dir)
            (local_dir / "model.gguf").write_bytes(_WEIGHTS[:16])  # some bytes land...
            # ...then nothing, ever. Bounded so a guard-less engine fails the
            # assertions below instead of hanging the suite.
            release.wait(20)

        try:
            result = _dl(tmp_path, snapshot_fn=wedged)
        finally:
            release.set()

        assert result.state == "failed"
        assert result.reason.startswith("stalled:no-bytes-in-"), result.reason
        assert len(attempts) == 2, "one retry, then surface"
        # Each attempt stages somewhere unique, so an abandoned fetch thread — which
        # cannot be stopped — can never write into a live attempt's tree.
        assert len(set(attempts)) == len(attempts)
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    def test_progressing_fetch_is_not_declared_stalled(self, tmp_path, test_model, monkeypatch):
        """A transfer still producing bytes must never be failed as stalled.

        The guard's real risk is the false positive, and it is silent: a clock that
        never re-arms, or one reading the ``total``-clamped count instead of the raw
        one, fails every healthy download identically. The live Hugging Face fetch
        cannot run in CI, so nothing else would catch it. Growth here deliberately
        runs PAST the manifest total, which is what pins the raw-vs-clamped half.
        """
        monkeypatch.setattr(dl, "_STALL_TIMEOUT_S", 0.4)
        finished = []

        def growing(repo, rev, local_dir, files):
            for i in range(6):  # ~0.9s of real progress, exceeding the manifest total
                (local_dir / f"part{i}.gguf").write_bytes(_WEIGHTS)
                time.sleep(0.15)
            (local_dir / "model.gguf").write_bytes(_WEIGHTS)
            finished.append(local_dir)

        result = _dl(tmp_path, snapshot_fn=growing)

        assert finished, "a fetch that was still producing bytes got abandoned"
        assert result.state == "installed", result.reason

    def test_cancel_lands_during_the_fetch(self, tmp_path, test_model):
        """Cancel must abandon an in-flight fetch, not wait it out.

        The stop flag used to be read only once the fetch returned, so Cancel could
        not land during the one step that takes minutes. The watch loop is the only
        place it can — and a cancelled download must not open a retry transfer.
        """
        stop = threading.Event()
        release = threading.Event()
        attempts: list = []

        def wedged(repo, rev, local_dir, files):
            attempts.append(local_dir)
            stop.set()  # the user hits Cancel as soon as the fetch starts
            release.wait(20)

        started = time.monotonic()
        try:
            result = _dl(tmp_path, snapshot_fn=wedged, stop_event=stop)
        finally:
            release.set()
        elapsed = time.monotonic() - started

        assert result.state == "cancelled"
        # Without the poll this still converges once `wedged` returns, so the timing
        # is what actually pins "lands DURING the fetch".
        assert elapsed < 5, f"cancel waited out the fetch instead of landing ({elapsed:.1f}s)"
        assert len(attempts) == 1, "a cancelled download must not start a retry"
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    def test_fetch_error_is_reported_not_swallowed(self, tmp_path, test_model):
        """An exception from the fetch must survive the worker-thread hand-back.

        The fetch runs on its own thread now, so its exception is captured there and
        re-raised on the caller's. Lose that hand-back and a network failure returns
        an empty staging dir instead — surfacing as ``missing-file``, which reads as
        a corrupt manifest and sends the user down the wrong path entirely.
        """

        def boom(repo, rev, local_dir, files):
            raise ConnectionError("no route to host")

        result = _dl(tmp_path, snapshot_fn=boom)

        assert result.state == "failed"
        assert result.reason == "download-error:ConnectionError"
        assert not list((tmp_path / _TEST_ID).glob(_RUNTIME + ".partial*"))

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
    def test_unpinned_variant_raises(self, tmp_path, monkeypatch):
        # An unpinned variant (PLACEHOLDER revision, no file hashes) is refused.
        unpinned = ModelSpec(
            id="unpinned-model",
            display_name="Unpinned",
            license="Apache-2.0",
            variants={_RUNTIME: Variant(
                repo="acme/unpinned", revision=registry.PLACEHOLDER,
                allowed_format="gguf", files=(),
            )},
        )
        monkeypatch.setitem(registry.MODELS, "unpinned-model", unpinned)
        with pytest.raises(ModelNotPinnedError):
            download_model(
                "unpinned-model", runtime=_RUNTIME, models_dir=tmp_path,
                snapshot_fn=_good_snapshot,
            )

    def test_shipped_default_model_is_release_pinned(self):
        # Regression guard: the shipped manifest must carry real revision SHAs and
        # per-file sha256 pins for every variant — a PLACEHOLDER here means every
        # user's onboarding download fails with "not-release-pinned".
        spec = registry.get_model(registry.DEFAULT_MODEL_ID)
        assert spec is not None
        assert spec.variants, "default model has no variants"
        for runtime, variant in spec.variants.items():
            assert variant.is_pinned(), (
                f"variant {runtime!r} of {registry.DEFAULT_MODEL_ID!r} is not "
                "release-pinned (PLACEHOLDER revision or missing file hashes)"
            )

    def test_not_installed_returns_none(self, tmp_path, test_model):
        assert get_installed_model_path(_TEST_ID, _RUNTIME, tmp_path) is None

    def test_unknown_model_fails(self, tmp_path):
        result = download_model("no-such-model", runtime="llamacpp", models_dir=tmp_path)
        assert result.state == "failed" and result.reason == "unknown-model"
