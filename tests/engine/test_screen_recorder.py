"""Contract tests for the ``engine.ScreenRecorder`` seam.

Slice 1 (SCR-37) introduced the seam shape; slice 2 (SCR-38) ported the
body of ``screencap.recorder.start_recording`` onto ``.run()``. The tests
here verify the contract that downstream slices will rely on, and
nothing more — no shape-of-dataclass assertions and no body behaviour
(parity with the wrapper is exercised by ``test_screen_recorder_parity``).
"""

from __future__ import annotations

import pytest


def test_construction_with_three_bundles() -> None:
    """``ScreenRecorder`` is constructed with three keyword-only bundles.

    The ADR locks down the seam shape: ``request=`` (what to record),
    ``channels=`` (how it talks to its environment), ``policies=``
    (how it behaves). Adding a future policy lands inside ``policies``
    without touching the constructor or its callers.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )

    request = RecordingRequest(name="demo", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=object(),
        lock=object(),
        menubar=object(),
        permission=object(),
        disk=object(),
        network=object(),
    )

    rec = ScreenRecorder(request=request, channels=channels, policies=policies)

    assert rec is not None  # construction succeeded; nothing else is in scope yet


def test_constructor_is_keyword_only() -> None:
    """ADR: ``ScreenRecorder(*, request, channels, policies)``.

    Positional construction is a hazard because future policy bundles
    extend ``RecordingPolicies``; positional callers would silently
    accept the wrong order.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )

    request = RecordingRequest(name="demo", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=object(), lock=object(), menubar=object(),
        permission=object(), disk=object(), network=object(),
    )

    with pytest.raises(TypeError):
        ScreenRecorder(request, channels, policies)  # type: ignore[misc]


def test_recording_policies_has_no_defaults() -> None:
    """ADR: ``RecordingPolicies`` has no defaults — fail-closed in production.

    Defaulting any policy to ``Noop`` would silently disable
    privacy/lock/signal handling. Tests opt into ``Noop`` via a helper
    in ``tests/helpers/policies.py`` (out of scope this slice).
    """
    from screencap.engine.screen_recorder import RecordingPolicies

    with pytest.raises(TypeError):
        RecordingPolicies()  # type: ignore[call-arg]


def test_error_hierarchy_matches_adr() -> None:
    """ADR: a two-tier error hierarchy under ``RecordingError``.

    ``PreflightError`` = recording never started; ``RecordingInterrupted``
    = stopped mid-recording (carries ``capture_dir`` + ``elapsed``).
    The CLI adapter (slice 8) translates these to exit codes; downstream
    slices raise the named subclasses by name. The tier split is the
    contract this test pins down.
    """
    from screencap.engine.screen_recorder import (
        DiskTooLowAtStart,
        NetworkPreflightFailed,
        OrphanProcessesFound,
        PermissionRevoked,
        PermissionsMissing,
        PreflightError,
        RecordingError,
        RecordingInterrupted,
    )

    assert issubclass(PreflightError, RecordingError)
    assert issubclass(RecordingInterrupted, RecordingError)

    # Preflight failures: recording never started, no capture_dir.
    for cls in (
        OrphanProcessesFound,
        PermissionsMissing,
        NetworkPreflightFailed,
        DiskTooLowAtStart,
    ):
        assert issubclass(cls, PreflightError), f"{cls} is not a PreflightError"

    # Mid-recording failures: capture_dir + elapsed available.
    assert issubclass(PermissionRevoked, RecordingInterrupted)
    # NB: ``LockContended`` (pidfile.py) and ``DiskFullError`` (recorder.py)
    # already exist in their pre-seam locations; the ADR re-exports them
    # from their owning policy modules in SCR-40 / SCR-42, not here.


def test_curated_surface_exposes_screen_recorder() -> None:
    """ADR AC: the seam is reachable through ``screencap.engine``.

    Bundle types are also exposed so callers can construct a
    ``ScreenRecorder`` without reaching past the curated surface.
    """
    import screencap.engine as engine

    for name in (
        "ScreenRecorder",
        "RecordingRequest",
        "IpcChannels",
        "RecordingPolicies",
        "RecordingResult",
    ):
        assert name in engine.__all__, f"{name} missing from engine.__all__"
        assert hasattr(engine, name), f"{name} not attribute-accessible on engine"
