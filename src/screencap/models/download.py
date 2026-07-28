"""Download + integrity engine for the local model (U4, KTD5).

Fetches a manifest-pinned model from Hugging Face and verifies it before marking
it installed. Every safety property is fail-closed — a hash mismatch, a disallowed
(executable) format, an interrupted/cancelled download, or insufficient disk
aborts and cleans up **symlink-safe**, leaving no directory the U2 provider's path
resolver would treat as installed.

Layout: ``<models_dir>/<model_id>/<runtime>/`` holds the verified files plus an
``.installed`` marker written **last**. A download stages into a uniquely-named
``.partial.*`` sibling and is renamed into place only after full verification, so
a crash never leaves a half-populated "installed" dir.

The fetch itself is bounded by a stall guard (:func:`_run_fetch_watched`) — the
upstream call has no deadline of its own — so a wedged transfer fails with a
distinct reason instead of blocking the daemon forever, and Cancel lands during
the fetch rather than after it. Both rest on bytes-on-disk being a live signal,
which is why :func:`_xet_disabled` forces the plain-HTTP transfer path.

The real Hugging Face fetch (:func:`_default_snapshot`) cannot run in CI; it is
injected (``snapshot_fn``) in tests and covered by manual runs. The verify /
perms / TOCTOU / stall / cancel / fail-closed / precheck / idempotency logic
below is CI-tested.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from screencap.models import registry
from screencap.models.registry import DEFAULT_MODEL_ID

log = logging.getLogger(__name__)

# Canonical Hugging Face host — pinned so an `HF_ENDPOINT` override cannot point
# the download at another host (the repo+commit+sha256 pin is only complete with
# the host pinned too).
_HF_ENDPOINT = "https://huggingface.co"

# Disk headroom multiplier over the manifest size — covers HF's cache-then-move
# double footprint plus a safety margin.
_DISK_HEADROOM = 2.2

_INSTALLED_MARKER = ".installed"

#: How often the staging dir is measured while the snapshot fetch blocks. Fast
#: enough to keep a progress bar visibly moving, cheap enough that walking a
#: handful of files costs nothing next to a multi-GB transfer.
_PROGRESS_SAMPLE_INTERVAL_S = 0.25

#: Wall-clock window with no new bytes on disk before a fetch is declared stalled.
#: Must clear two floors. (1) ``huggingface_hub``'s own retry chain: five retries
#: of a 10 s socket timeout plus a 1 s backoff, ~55 s per file. (2) The
#: granularity of the signal itself: hf streams in 10 MiB chunks, so the staged
#: byte count advances once per chunk — ~50 s apart at 200 KB/s. Five minutes
#: clears both; the hard floor it implies is 10 MiB / 300 s ~= 35 KB/s, below
#: which a healthy transfer would be failed — a rate at which the ~2 GB model
#: takes 17 hours, so nothing usable is being given up.
#:
#: Both floors assume the transfer actually streams into the staging dir, which
#: is why :func:`_xet_disabled` forces the plain-HTTP path. See its docstring.
_STALL_TIMEOUT_S = 300.0

#: Fetch attempts before a stall is surfaced — i.e. one retry. A stall is usually
#: a wedged connection, and a fresh fetch re-resolves DNS and opens new sockets,
#: which is the cheapest recovery that needs nothing from the user. Only one,
#: though: the stalled fetch cannot be stopped (see :func:`_run_fetch_watched`),
#: so every retry stacks on a still-live transfer, and past the second the wasted
#: sockets and disk outweigh the odds of the next one landing.
_FETCH_ATTEMPTS = 2

ProgressCb = Callable[[int, int], None]  # (bytes_done, bytes_total)


class ModelNotPinnedError(RuntimeError):
    """The variant's revision / file hashes are not release-pinned yet (KTD5)."""


class _FetchStalled(RuntimeError):
    """No new bytes reached the staging dir within the stall window."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"no new bytes in {seconds:.0f}s")
        self.seconds = seconds


class _FetchCancelled(RuntimeError):
    """The stop flag was set while the fetch was in flight."""


@dataclass(frozen=True)
class DownloadResult:
    state: str  # "installed" | "failed" | "cancelled"
    model_id: str
    runtime: str
    path: Path | None = None
    reason: str | None = None
    already_installed: bool = False


def _variant_dir(models_dir: Path, model_id: str, runtime: str) -> Path:
    return models_dir / model_id / runtime


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rmtree_safe(path: Path) -> None:
    """Remove ``path`` without following symlinks out of it."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path, ignore_errors=True)


def _staged_bytes(staging: Path) -> int:
    """Bytes materialized under ``staging`` so far.

    Counts regular files only, via ``lstat`` so a symlink is never followed out of
    the staging dir (the verify pass refuses symlinks outright; this must not
    inflate the count with a link target's size in the meantime). Hugging Face
    stages each file as a ``.incomplete`` temp under ``.cache/huggingface`` and
    then *renames* it into place, so a file's bytes are counted once either way.
    """
    total = 0
    for p in staging.rglob("*"):
        try:
            st = p.lstat()
        except OSError:
            continue  # vanished mid-walk (a rename into place) — next sample gets it
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


#: Abandoned fetch threads and the staging dirs they may still be writing to.
#: A stalled fetch cannot be stopped, so its tree cannot be reclaimed while the
#: thread lives — ``rmtree`` would race the writer and lose (the recreated
#: subdir makes the final ``rmdir`` fail with ENOTEMPTY, swallowed by
#: ``ignore_errors``). Reaped once the thread finally dies; without this the
#: tree outlives every later download, since an installed model returns from
#: :func:`download_model` before any staging dir is created.
_orphan_fetches: list[tuple[threading.Thread, Path]] = []
_orphan_lock = threading.Lock()

# Reentrancy guard for the process-global Xet switch. Depth-counted rather than
# save/restore-per-call because an abandoned (stalled) fetch thread can still be
# inside the block when a retry enters it: a naive restore would let the late
# exit write the *pinned* value back as if it were the original, leaving the
# setting permanently flipped for the daemon's other hub consumers.
_xet_lock = threading.Lock()
_xet_depth = 0
_xet_saved = False


@contextmanager
def _xet_disabled() -> Iterator[None]:
    """Force the plain-HTTP transfer path for the duration of a fetch.

    Xet downloads chunks into a **global** cache and assembles the file only at
    the end, so the staging dir stays flat for essentially the whole transfer —
    measured on the pinned 1.7 GB weights, staged bytes moved once in 45 s,
    against a 3.5 s longest plateau over the plain HTTP path. Both shipped
    variants are Xet-backed, so without this every bytes-on-disk reading is dead:
    progress bars sit at 0% for the entire multi-GB fetch (the QA "stuck at 0%"
    report), and the stall clock starves and fails a perfectly healthy download.

    ``is_xet_available`` reads this constant at *call* time, so setting it here
    works whether or not ``huggingface_hub`` was already imported elsewhere in
    the process — unlike the ``HF_ENDPOINT`` environment variable, which is read
    once at import (hence the per-call ``endpoint=`` argument below).
    """
    global _xet_depth, _xet_saved
    from huggingface_hub import constants as hf_constants

    with _xet_lock:
        if _xet_depth == 0:
            _xet_saved = hf_constants.HF_HUB_DISABLE_XET
        _xet_depth += 1
        hf_constants.HF_HUB_DISABLE_XET = True
    try:
        yield
    finally:
        with _xet_lock:
            _xet_depth -= 1
            if _xet_depth == 0:
                hf_constants.HF_HUB_DISABLE_XET = _xet_saved


def _reclaim_staging(target: Path) -> None:
    """Remove leftover staging dirs no live fetch is still writing to.

    Covers three sources: a crashed process, an abandoned fetch whose thread has
    since died, and the previous attempt of the current call. Skips any dir a
    *live* orphan owns — deleting under an active writer accomplishes nothing and
    leaves a partially-recreated tree behind.
    """
    with _orphan_lock:
        live = {staging for thread, staging in _orphan_fetches if thread.is_alive()}
        _orphan_fetches[:] = [(t, s) for t, s in _orphan_fetches if t.is_alive()]
    for stale in target.parent.glob(target.name + ".partial*"):
        if stale not in live:
            _rmtree_safe(stale)


def _new_staging(target: Path) -> Path:
    """Create a fresh, uniquely-named staging dir for one download attempt.

    Unique per attempt because a stalled fetch is *abandoned*, not stopped: its
    thread can still be writing. Under a fixed ``<runtime>.partial`` path those
    late writes would land in the *next* attempt's staging dir — after the sha256
    pass verified it, and while it is being renamed into place — reopening exactly
    the TOCTOU that pass exists to close. A unique dir keeps an orphan confined to
    a tree nobody will ever install.

    Uniqueness costs nothing: the engine has never resumed a partial download (the
    old fixed path was unconditionally removed at the start of every attempt), so
    leftovers are reclaimed by :func:`_reclaim_staging` instead.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    _reclaim_staging(target)
    return Path(  # mkdtemp creates 0o700 atomically, which os.replace carries to target
        tempfile.mkdtemp(prefix=target.name + ".partial.", dir=parent)
    )


def _run_fetch_watched(
    fetch: Callable[[Path], None],
    staging: Path,
    total: int,
    progress_cb: ProgressCb | None,
    *,
    stall_timeout_s: float,
    stop_event: threading.Event | None = None,
    interval: float = _PROGRESS_SAMPLE_INTERVAL_S,
) -> None:
    """Run the blocking fetch on a worker thread, watched from the caller's.

    ``snapshot_download`` is one blocking call with no byte-level callback *and*
    no overall deadline, so run inline it can both report nothing for an entire
    multi-GB transfer and never return at all. One measurement answers both —
    bytes materialized under the staging dir, sampled on a fixed cadence:

    - **Progress**: each sample is reported through ``progress_cb``, so a bar moves
      instead of sitting at 0% for minutes, which no user can tell apart from a
      hang (the QA "stuck at 0%" report). Sampling the dir keeps the reading
      independent of ``huggingface_hub`` internals — its ``tqdm_class`` seam only
      counts *files*, useless when one weight file is ~99% of the bytes.
    - **Stall**: if the count does not advance for ``stall_timeout_s``, the fetch
      is abandoned and :class:`_FetchStalled` is raised. Nothing upstream imposes
      a deadline — hf's per-file retry budget is reset by every chunk that
      arrives, so a trickling connection never exhausts it — and without one a
      wedged transfer blocks forever: the daemon job stays ``running``, the status
      verb keeps saying ``downloading``, and the idle-shutdown busy predicate pins
      the process.
    - **Cancel**: the same loop polls ``stop_event`` and abandons the fetch, so
      Cancel lands within one sample instead of waiting out the whole transfer.
      This is the only place in the module where it *can* land mid-fetch; the
      surrounding checks are between steps, and the fetch is the long step.

    The stall clock reads the **raw** count, never the ``total``-clamped one a
    consumer sees: a staging dir that grows past the manifest total (hf writes
    ``.cache/huggingface`` metadata sidecars alongside the files) would pin the
    clamped value and read as a stall on a perfectly healthy transfer.

    An abandoned fetch keeps running — a blocking socket read cannot be
    interrupted from outside — so it is a daemon thread, registered in
    :data:`_orphan_fetches` so its staging dir is reclaimed once it dies.

    ``daemon=True`` is necessary but **not sufficient** to let the process exit:
    ``snapshot_download`` fans out through a ``ThreadPoolExecutor`` whose workers
    are non-daemon and are joined at interpreter shutdown, so a still-wedged
    orphan blocks exit (measured). That is unchanged from before this guard
    existed — the difference is that the daemon now reaches a terminal state and
    un-pins its busy predicate instead of reporting ``downloading`` forever.
    Fully killable teardown needs subprocess isolation; tracked separately.

    Reporting happens only on this thread, so a sample can never land after the
    caller's terminal reading and drag the reported byte count backwards.
    """
    failure: list[BaseException] = []

    def _work() -> None:
        try:
            fetch(staging)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            failure.append(exc)

    thread = threading.Thread(target=_work, name="model-download-fetch", daemon=True)
    thread.start()

    def _abandon(reason: BaseException) -> None:
        with _orphan_lock:
            _orphan_fetches.append((thread, staging))
        raise reason

    last_bytes = -1  # never equal to a real reading, so the first sample arms the clock
    last_change = time.monotonic()
    while True:
        thread.join(interval)
        if not thread.is_alive():
            break
        if stop_event is not None and stop_event.is_set():
            _abandon(_FetchCancelled())
        staged = _staged_bytes(staging)
        now = time.monotonic()
        if staged != last_bytes:
            last_bytes = staged
            last_change = now
        elif now - last_change >= stall_timeout_s:
            _abandon(_FetchStalled(now - last_change))
        if progress_cb is not None:
            try:
                progress_cb(min(staged, total), total)
            except Exception:  # a broken consumer must never fail the download
                log.debug("model download progress sample failed", exc_info=True)

    if failure:
        raise failure[0]


def _fetch_with_stall_retry(
    target: Path,
    fetch: Callable[[Path], None],
    total: int,
    progress_cb: ProgressCb | None,
    stop_event: threading.Event | None,
) -> Path:
    """Fetch into a fresh staging dir, retrying past a stall (:data:`_FETCH_ATTEMPTS`).

    Returns the staging dir holding the fetched files. On failure it raises,
    having already removed every staging dir it created — except one an abandoned
    fetch still owns, which :func:`_reclaim_staging` takes once that thread dies.
    So the caller only ever owns cleanup of the dir it gets back.

    A cancel is never retried: opening a second multi-GB transfer after the user
    pressed Cancel is worse than the stall it would be recovering from.

    A retry restarts from zero bytes, so the reported progress drops back toward
    0%. That is the honest reading: the transfer really did start over.
    """
    final_attempt = _FETCH_ATTEMPTS - 1
    for attempt in range(_FETCH_ATTEMPTS):
        staging = _new_staging(target)
        try:
            _run_fetch_watched(
                fetch,
                staging,
                total,
                progress_cb,
                stall_timeout_s=_STALL_TIMEOUT_S,
                stop_event=stop_event,
            )
            return staging
        except _FetchCancelled:
            raise  # abandoned: the orphan owns `staging` until its thread dies
        except _FetchStalled as exc:
            if attempt == final_attempt:
                raise  # ditto — abandoned, so reclaiming it now would race the writer
            log.warning("model download stalled (%s); retrying once from scratch", exc)
        except BaseException:
            _rmtree_safe(staging)  # the fetch thread is dead, so the tree is ours
            raise
    raise AssertionError("unreachable: the final attempt re-raises")  # pragma: no cover


def get_disclosed_size(model_id: str | None = None, runtime: str | None = None) -> int | None:
    """Manifest download size (bytes) for pre-download disclosure, or ``None``."""
    spec = registry.get_model(model_id or DEFAULT_MODEL_ID)
    if spec is None:
        return None
    rt = runtime or _select_runtime()
    variant = spec.variant_for(rt)
    return variant.size_bytes if variant and variant.files else None


def is_model_installed(
    model_id: str | None = None,
    runtime: str | None = None,
    models_dir: Path | None = None,
) -> bool:
    return get_installed_model_path(model_id, runtime, models_dir) is not None


def get_installed_model_path(
    model_id: str | None = None,
    runtime: str | None = None,
    models_dir: Path | None = None,
) -> Path | None:
    """Return the installed model dir for the host runtime, or ``None``.

    "Installed" means the variant dir exists with its ``.installed`` marker (the
    marker is written only after full verification). Used by the U2 provider.
    """
    from screencap import config

    mid = model_id or DEFAULT_MODEL_ID
    rt = runtime or _select_runtime()
    base = models_dir if models_dir is not None else config.get_models_dir()
    target = _variant_dir(base, mid, rt)
    marker = target / _INSTALLED_MARKER
    if target.is_dir() and not target.is_symlink() and marker.is_file():
        return target
    return None


def _select_runtime() -> str:
    from screencap.segmentation.local_model.runtime import select_runtime

    return select_runtime()


def _default_snapshot(repo: str, revision: str, local_dir: Path, filenames: list[str]) -> None:
    """Live Hugging Face fetch (host-pinned, real files). Not run in CI."""
    from huggingface_hub import snapshot_download

    with _xet_disabled():  # keep the transfer visible on disk (see the docstring)
        snapshot_download(
            repo_id=repo,
            revision=revision,
            local_dir=str(local_dir),
            local_dir_use_symlinks=False,  # materialize real files (close TOCTOU)
            allow_patterns=filenames or None,
            # Pin the host per call. The HF_ENDPOINT env var is read once at
            # import, so setting it here would be inert in a daemon that already
            # imported the hub — the repo+commit+sha256 pin is only complete with
            # the host pinned too, so it has to be an argument.
            endpoint=_HF_ENDPOINT,
        )


def download_model(
    model_id: str | None = None,
    *,
    runtime: str | None = None,
    models_dir: Path | None = None,
    progress_cb: ProgressCb | None = None,
    stop_event: threading.Event | None = None,
    snapshot_fn: Callable[[str, str, Path, list[str]], None] | None = None,
) -> DownloadResult:
    """Download + verify the model, or return a fail-closed result. See module docs."""
    from screencap import config

    mid = model_id or DEFAULT_MODEL_ID
    rt = runtime or _select_runtime()
    base = models_dir if models_dir is not None else config.get_models_dir()
    snapshot = snapshot_fn or _default_snapshot

    def _fail(reason: str) -> DownloadResult:
        log.warning("model download failed (%s/%s): %s", mid, rt, reason)
        return DownloadResult("failed", mid, rt, reason=reason)

    spec = registry.get_model(mid)
    if spec is None:
        return _fail("unknown-model")
    variant = spec.variant_for(rt)
    if variant is None:
        return _fail(f"no-variant-for-runtime:{rt}")
    if not variant.is_pinned():
        # Plumbing ships, but an unverified model is never fetched (KTD5).
        raise ModelNotPinnedError(
            f"model {mid!r} variant {rt!r} is not release-pinned in this build"
        )

    target = _variant_dir(base, mid, rt)
    # Before the already-installed short-circuit: an abandoned fetch from an
    # earlier call may have left a multi-GB tree, and once the model installs this
    # is the only line that ever runs again.
    _reclaim_staging(target)
    if get_installed_model_path(mid, rt, base) is not None:
        return DownloadResult("installed", mid, rt, path=target, already_installed=True)

    # Free-space precheck (coordinate with the recording disk stop threshold).
    required = int(variant.size_bytes * _DISK_HEADROOM)
    try:
        free = shutil.disk_usage(base).free
    except OSError:
        free = required  # can't check → proceed; verification still fail-closes
    if free < required:
        need_gb = required / (1024**3)
        return _fail(f"insufficient-disk:need-{need_gb:.1f}GB-free")

    if stop_event is not None and stop_event.is_set():
        return DownloadResult("cancelled", mid, rt)

    total = variant.size_bytes
    if progress_cb:
        progress_cb(0, total)

    filenames = [f.name for f in variant.files]

    def _fetch(into: Path) -> None:
        snapshot(variant.repo, variant.revision, into, filenames)

    try:
        staging = _fetch_with_stall_retry(target, _fetch, total, progress_cb, stop_event)
    except _FetchCancelled:
        return DownloadResult("cancelled", mid, rt)
    except _FetchStalled as exc:
        # Distinct and greppable, and terminal: the UI maps any `failed` reason to
        # its Retry affordance, so the user gets a way out instead of a bar that
        # stopped moving. `_fetch_with_stall_retry` owns the staging cleanup.
        return _fail(f"stalled:no-bytes-in-{exc.seconds:.0f}s")
    except Exception as exc:  # network / disk / snapshot error
        return _fail(f"download-error:{type(exc).__name__}")

    try:
        if stop_event is not None and stop_event.is_set():
            _rmtree_safe(staging)
            return DownloadResult("cancelled", mid, rt)

        # Reject any executable-on-load format before touching a file (KTD5/H1).
        for p in staging.rglob("*"):
            if p.is_file() and registry.ext_is_disallowed(p.name):
                _rmtree_safe(staging)
                return _fail(f"disallowed-format:{p.name}")

        # Verify each manifest file: present, not a symlink (TOCTOU), sha256 match.
        for fspec in variant.files:
            fpath = staging / fspec.name
            if not fpath.exists():
                _rmtree_safe(staging)
                return _fail(f"missing-file:{fspec.name}")
            if fpath.is_symlink():
                _rmtree_safe(staging)
                return _fail(f"symlink-refused:{fspec.name}")
            digest = _sha256(fpath)
            if digest != fspec.sha256:
                _rmtree_safe(staging)
                return _fail(f"sha256-mismatch:{fspec.name}")
            try:
                fpath.chmod(0o600)
            except OSError:
                pass
    except Exception as exc:  # disk / verification error (the fetch is already done)
        _rmtree_safe(staging)
        return _fail(f"download-error:{type(exc).__name__}")

    # Atomic-ish install: swap staging into place, then write the marker LAST.
    _rmtree_safe(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, target)
    (target / _INSTALLED_MARKER).write_text("ok")
    if progress_cb:
        progress_cb(total, total)
    log.info("model installed: %s/%s at %s", mid, rt, target)
    return DownloadResult("installed", mid, rt, path=target)
