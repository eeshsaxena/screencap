"""``engine.ScreenRecorder`` seam — skeleton (SCR-37, slice 1 of SCR-31).

This module declares the *shape* of the seam that will absorb the
1,900-line ``screencap/recorder.py`` wrapper. No logic has moved yet:
``.run()`` raises ``NotImplementedError`` until SCR-38 lands the
functional shim, and per-policy logic arrives in SCR-39 through SCR-45.

The full design lives in
``docs/decisions/0001-engine-screen-recorder-seam.md``. Everything in
this file should reflect that ADR; if they disagree, update one or both
deliberately.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from screencap.engine.config import RecordingConfig


class RecordingError(Exception):
    """Base for all seam-raised exceptions. Programming bugs propagate unchanged."""


class PreflightError(RecordingError):
    """Recording never started. CLI adapter translates to a non-zero exit."""


class OrphanProcessesFound(PreflightError):
    """``--force-clean`` is required to terminate orphaned recorder processes."""


class PermissionsMissing(PreflightError):
    """One or more TCC permissions (Screen Recording / Accessibility / Input) absent."""


class NetworkPreflightFailed(PreflightError):
    """V1.5 KEK/DEK setup or mitm proxy bring-up failed before recording started."""


class DiskTooLowAtStart(PreflightError):
    """Free disk space below the configured ``stop_mb`` threshold at startup."""


class RecordingInterrupted(RecordingError):
    """Stopped mid-recording. Subclasses carry ``capture_dir`` + ``elapsed``."""

    capture_dir: Path
    elapsed: float


class PermissionRevoked(RecordingInterrupted):
    """A required TCC permission was revoked during recording."""


class Monitor(Protocol):
    """Shared shape for ``PermissionPolicy`` and ``DiskPolicy``.

    Both observe an external condition on a cadence, raise
    ``PreflightError`` at startup if absent, and ``RecordingInterrupted``
    if the condition disappears mid-recording.
    """

    def preflight(self) -> None: ...

    def poll(self, now: float) -> None: ...

    @property
    def next_poll_at(self) -> float: ...


class SignalPolicy(Protocol):
    """SIGINT / SIGTERM handler installation. ``ThreeTapSigint`` | ``Noop``."""


class LockPolicy(Protocol):
    """Pidfile lifecycle. ``ClaimLock`` | ``InheritLock`` | ``Noop``."""


class MenubarPolicy(Protocol):
    """Menubar subprocess + queue lifecycle. ``SpawnNewMenubar`` | ``Noop``."""


class PermissionPolicy(Monitor, Protocol):
    """TCC permission preflight + revocation polling. ``MacOSTCC`` | ``Noop``."""


class DiskPolicy(Monitor, Protocol):
    """Disk-space preflight + adaptive polling. ``MonitorAndStop`` | ``Noop``."""


class NetworkPolicy(Protocol):
    """V1.5 KEK/DEK + mitm preflight + proxy lifecycle. ``MitmProxyV15`` | ``Noop``."""


@dataclass(frozen=True, slots=True)
class RecordingRequest:
    """What kind of recording is this."""

    name: str
    config: RecordingConfig
    description: str | None = None
    cloud_intent: bool = False
    keep_local: bool = True
    intent_source: str = "flag"
    segmentation_mode: str = "llm"
    scrub_enabled: bool = True
    show_on_website: bool = True


@dataclass(frozen=True, slots=True)
class IpcChannels:
    """How this recording talks to its environment."""

    window_feed: mp.Queue
    override: mp.Queue
    disable: mp.Queue

    @classmethod
    def create(cls) -> IpcChannels:
        return cls(mp.Queue(), mp.Queue(), mp.Queue())


@dataclass(frozen=True, slots=True)
class RecordingPolicies:
    """How this recording behaves.

    No defaults — production must be explicit. Defaulting any field to
    ``Noop`` would silently disable lock/signal/privacy. Tests get an
    ``all_noop_policies()`` helper outside the production package.
    """

    signal: SignalPolicy
    lock: LockPolicy
    menubar: MenubarPolicy
    permission: PermissionPolicy
    disk: DiskPolicy
    network: NetworkPolicy


@dataclass(frozen=True, slots=True)
class RecordingResult:
    """Returned by ``ScreenRecorder.run()`` on clean stop."""

    capture_dir: Path
    elapsed: float


class ScreenRecorder:
    """Recording seam — skeleton until SCR-38.

    Intended use: ``with ScreenRecorder(...) as rec: rec.run()``. The
    context manager owns setup/teardown; ``.run()`` blocks until stop.
    See the ADR for the full lifecycle contract.
    """

    def __init__(
        self,
        *,
        request: RecordingRequest,
        channels: IpcChannels,
        policies: RecordingPolicies,
    ) -> None:
        self._request = request
        self._channels = channels
        self._policies = policies

    def run(self) -> RecordingResult:
        raise NotImplementedError("ScreenRecorder.run() lands in SCR-38")
