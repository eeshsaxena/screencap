"""Behavioral tests for the U5 destination-agnostic stage runner.

``screencap.pipeline_stages.PipelineStageRunner`` runs transcribe → export
events → manifest exactly once per chunk, ledger-driven and idempotent,
with NO local-vs-cloud knowledge in its own code (R4). These tests pin:

  - happy path: all three stages run once, artifacts on disk, ledger
    STAGED / stages_state DONE;
  - idempotent: a re-run on a STAGED chunk is a no-op (the expensive
    transcribe step is NOT re-invoked — asserted via a call-count spy);
  - partial failure: a failing manifest leaves the chunk pre-STAGED so a
    re-run completes it (no false "done");
  - destination-agnostic: the runner's export output is identical for a
    would-be-local vs would-be-cloud recording (the runner has no
    destination input — the injected export step is the only difference).

Run::

    PYTHONPATH=src python -m pytest tests/test_pipeline_stages.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from screencap import pipeline_state as ps
from screencap.pipeline_stages import PipelineStageRunner, StageArtifacts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger(recording_db) -> ps.PipelineLedger:
    """A PipelineLedger over the real engine recording.db fixture."""
    db_path = recording_db.db_path
    ps.ensure_pipeline_state_schema(db_path)
    return ps.PipelineLedger(db_path)


@pytest.fixture
def capture_dir(recording_db) -> Path:
    return recording_db.db_path.parent


class _Spy:
    """A counting/recording stand-in for an injected stage step.

    Writes its named artifact (idempotently — early-returns if it exists)
    so the runner's on-disk idempotency and the spy's call count together
    prove "exactly once".
    """

    def __init__(self, capture_dir: Path, kind: str):
        self._dir = capture_dir
        self._kind = kind
        self.calls = 0

    def _path(self, idx: int) -> Path:
        names = {
            "transcript": f"transcript_{idx:04d}.txt",
            "events": f"events_{idx:04d}.jsonl",
            "manifest": f"chunk_{idx:04d}_manifest.json",
        }
        return self._dir / names[self._kind]

    def transcribe(self, idx: int):
        self.calls += 1
        p = self._path(idx)
        if not p.exists():
            p.write_text("transcript")
        return p

    def export(self, idx: int, s: float, e: float):
        self.calls += 1
        p = self._path(idx)
        if not p.exists():
            p.write_text("{}")
        return p

    def manifest(self, idx: int, s: float, e: float):
        self.calls += 1
        p = self._path(idx)
        if not p.exists():
            p.write_text("{}")
        return p


def _runner(capture_dir, ledger, *, transcribe, export, manifest):
    return PipelineStageRunner(
        capture_dir,
        transcribe=transcribe.transcribe,
        export_events=export.export,
        manifest=manifest.manifest,
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_all_stages_run_once_artifacts_and_ledger_staged(
        self, capture_dir, ledger
    ):
        t = _Spy(capture_dir, "transcript")
        x = _Spy(capture_dir, "events")
        m = _Spy(capture_dir, "manifest")
        runner = _runner(capture_dir, ledger, transcribe=t, export=x, manifest=m)

        ledger.seed_chunk(0)
        artifacts = runner.run_chunk(0, 1000.0, 1001.0)

        assert isinstance(artifacts, StageArtifacts)
        # Each stage ran exactly once.
        assert (t.calls, x.calls, m.calls) == (1, 1, 1)
        # Artifacts exist on disk.
        assert artifacts.transcript.exists()
        assert artifacts.events.exists()
        assert artifacts.manifest.exists()
        # Ledger reflects STAGED / stages_state DONE.
        row = ledger.get_chunk(0)
        assert row.lifecycle == ps.Lifecycle.STAGED
        assert row.stages_state == ps.StageState.DONE


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotent:
    def test_rerun_on_staged_chunk_is_noop_no_retranscribe(
        self, capture_dir, ledger
    ):
        t = _Spy(capture_dir, "transcript")
        x = _Spy(capture_dir, "events")
        m = _Spy(capture_dir, "manifest")
        runner = _runner(capture_dir, ledger, transcribe=t, export=x, manifest=m)

        ledger.seed_chunk(0)
        runner.run_chunk(0, 1000.0, 1001.0)
        assert t.calls == 1

        # Second invocation: the ledger STAGED gate short-circuits BEFORE
        # any step runs — the expensive transcribe is NOT re-invoked.
        result = runner.run_chunk(0, 1000.0, 1001.0)
        assert result is None  # idempotent no-op signal
        assert (t.calls, x.calls, m.calls) == (1, 1, 1)

    def test_no_ledger_falls_back_to_on_disk_idempotency(self, capture_dir):
        """Without a ledger, each step's own on-disk early-return is the
        only idempotency — the runner still runs but the steps no-op on
        existing artifacts. Here the spies write-once, so a re-run re-calls
        the step but the artifact is unchanged."""
        t = _Spy(capture_dir, "transcript")
        x = _Spy(capture_dir, "events")
        m = _Spy(capture_dir, "manifest")
        runner = PipelineStageRunner(
            capture_dir,
            transcribe=t.transcribe,
            export_events=x.export,
            manifest=m.manifest,
            ledger=None,
        )
        runner.run_chunk(0, 1000.0, 1001.0)
        # No ledger → no STAGED gate → steps run again (their own on-disk
        # early-return is the real-world idempotency; the spy still counts).
        runner.run_chunk(0, 1000.0, 1001.0)
        assert t.calls == 2  # re-invoked, but artifact unchanged
        assert (capture_dir / "transcript_0000.txt").read_text() == "transcript"


# ---------------------------------------------------------------------------
# Partial / failed manifest is not a false "done"
# ---------------------------------------------------------------------------

class TestPartialFailure:
    def test_failing_manifest_leaves_chunk_pre_staged(self, capture_dir, ledger):
        t = _Spy(capture_dir, "transcript")
        x = _Spy(capture_dir, "events")

        calls = {"n": 0}

        def failing_then_ok_manifest(idx, s, e):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("manifest boom")
            p = capture_dir / f"chunk_{idx:04d}_manifest.json"
            p.write_text("{}")
            return p

        runner = PipelineStageRunner(
            capture_dir,
            transcribe=t.transcribe,
            export_events=x.export,
            manifest=failing_then_ok_manifest,
            ledger=ledger,
        )
        ledger.seed_chunk(0)

        # First run: manifest raises → NOT marked STAGED.
        with pytest.raises(RuntimeError, match="manifest boom"):
            runner.run_chunk(0, 1000.0, 1001.0)
        row = ledger.get_chunk(0)
        assert row.lifecycle == ps.Lifecycle.PENDING
        assert row.stages_state == ps.StageState.PENDING

        # Re-run completes (transcribe/export early-return on existing
        # artifacts; manifest now succeeds) → STAGED.
        artifacts = runner.run_chunk(0, 1000.0, 1001.0)
        assert artifacts is not None
        row = ledger.get_chunk(0)
        assert row.lifecycle == ps.Lifecycle.STAGED
        assert row.stages_state == ps.StageState.DONE


# ---------------------------------------------------------------------------
# Destination-agnostic: identical output for would-be-local vs would-be-cloud
# ---------------------------------------------------------------------------

class TestDestinationAgnostic:
    def test_runner_has_no_destination_input(self, capture_dir, ledger):
        """The runner's API takes no destination — the only difference
        between local and cloud is the injected export step's content. Two
        runners built with the SAME injected steps over the SAME inputs
        produce byte-identical export artifacts, proving the runner carries
        no local-vs-cloud branch of its own (R4)."""
        # "local" run.
        local_dir = capture_dir / "local"
        cloud_dir = capture_dir / "cloud"
        local_dir.mkdir()
        cloud_dir.mkdir()

        def make_steps(d):
            return _Spy(d, "transcript"), _Spy(d, "events"), _Spy(d, "manifest")

        lt, lx, lm = make_steps(local_dir)
        ct, cx, cm = make_steps(cloud_dir)

        # Same agnostic export step content for both — the runner cannot
        # distinguish destinations; only an externally-injected cloud filter
        # (NOT present in the runner) would differ.
        local_runner = PipelineStageRunner(
            local_dir, transcribe=lt.transcribe,
            export_events=lx.export, manifest=lm.manifest, ledger=None,
        )
        cloud_runner = PipelineStageRunner(
            cloud_dir, transcribe=ct.transcribe,
            export_events=cx.export, manifest=cm.manifest, ledger=None,
        )
        la = local_runner.run_chunk(0, 1000.0, 1001.0)
        ca = cloud_runner.run_chunk(0, 1000.0, 1001.0)

        assert la.events.read_bytes() == ca.events.read_bytes()
        assert la.manifest.read_bytes() == ca.manifest.read_bytes()
