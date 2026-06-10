"""Upload recordings to GCS via signed URLs from a Cloud Function."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

from screencap._stderr_events import (
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_FILE_DONE,
    EVENT_UPLOAD_FINISHED,
    EVENT_UPLOAD_STARTED,
    emit_event,
)
from screencap.config import get_recordings_dir


def _raise_keyboard_interrupt(signum, frame):
    """SIGTERM handler installed during upload_recording.

    SIGTERM does not raise KeyboardInterrupt by default in CPython (only
    SIGINT does), so the SwiftUI shell's window-close-as-cancel path
    (which terminate()s the subprocess) would otherwise kill the process
    silently with no chance to emit upload_failed or run cleanup. This
    handler converts SIGTERM into the same KeyboardInterrupt the existing
    Ctrl+C path already handles, giving us one cancel codepath. Must be
    installed on the main thread (signal.signal restriction).
    """
    raise KeyboardInterrupt

console = Console()

UPLOAD_STATUS_FILE = ".upload_status.json"

DEFAULT_UPLOAD_URL = (
    "https://get-upload-urls-ld7izzjvga-rj.a.run.app"
)

CONTENT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".flac": "audio/flac",
    ".db": "application/x-sqlite3",
    ".json": "application/json",
    ".txt": "text/plain",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".csv": "text/csv",
    ".jsonl": "application/x-ndjson",
}


def _get_upload_url() -> str:
    return os.environ.get("SCREENCAP_UPLOAD_URL", DEFAULT_UPLOAD_URL)


def _content_type(path: Path) -> str:
    ct = CONTENT_TYPES.get(path.suffix.lower())
    if ct:
        return ct
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


@dataclass
class FileInfo:
    name: str
    path: Path
    content_type: str
    size: int


@dataclass
class UploadResult:
    recording: str
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    total_bytes: int = 0
    gcs_prefix: str = ""


def _fmt_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    if nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    return f"{nbytes / (1024 * 1024 * 1024):.1f} GB"


def _write_upload_status(recording_dir: Path, result: UploadResult) -> None:
    """Write upload status marker to recording directory atomically."""
    status = {
        "uploaded_at": datetime.now().isoformat(),
        "gcs_prefix": result.gcs_prefix,
        "files_uploaded": len(result.uploaded),
        "files_skipped": len(result.skipped),
        "total_bytes": result.total_bytes,
    }
    final_path = recording_dir / UPLOAD_STATUS_FILE
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(status, indent=2))
    os.replace(tmp_path, final_path)


def is_uploaded(recording_dir: Path) -> bool:
    """Check if a recording has been uploaded."""
    return (recording_dir / UPLOAD_STATUS_FILE).is_file()


def _wal_checkpoint(recording_dir: Path) -> None:
    """Checkpoint recording.db WAL to ensure a clean DB file for upload."""
    from screencap.recording_db import open_recording_db

    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return
    try:
        with open_recording_db(db_path, read_only=False) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass  # best-effort — upload proceeds even if checkpoint fails


# ---------------------------------------------------------------------------
# R8 / U2: the single upload-seam exclusion rule.
#
# The unified pipeline uploads from the SOURCE dir, where the raw `recording.db`
# (unscrubbed PII — and, per U1, the home of the local-only `pipeline_chunk_state`
# ledger) is present. Previously the raw DB stayed out of the cloud only by
# *architecture* (the live path's `checkpoint_and_upload_db` skip + the post-hoc
# path uploading a separate scrubbed sibling dir). The unified pipeline removes
# both structural guards, so THIS exclusion is the only thing standing between
# raw PII and GCS. It is a hard safety gate, enforced two ways:
#
#   1. `list_recording_files` never enumerates a raw artifact (matched by FULL
#      relative path, since it rglobs subdirs — a nested `recording.db` is
#      excluded too, not just a top-level basename).
#   2. `assert_uploadable` REJECTS (raises) any explicit attempt to enqueue a
#      raw artifact, so a caller bypassing `list_recording_files` still hits the
#      gate — a raw artifact is never *silently* dropped.
#
# Denylist (not allowlist), deliberately: the eligible-artifact set is open-ended
# (chunk media, audio, scrubbed events/transcript/manifest, screenshots, the
# completion sentinel, session_summary/profiling/system_metrics, the `_unlisted`
# marker, `viewer.html`, legacy non-chunked `video.mp4`/`events.jsonl`, and future
# masked-chunk copies). An incomplete allowlist would silently stop legitimate
# artifacts from uploading — the one failure mode the task forbids. The denylist
# is narrow and exhaustive over RAW artifacts, and adding a new raw artifact is a
# one-line change here. Raw artifacts are:
#   - `recording.db`               — unscrubbed PII + local-only ledger (R8)
#   - `recording.db-wal` / `-shm`  — SQLite sidecars of the same DB
#   - `*.scrub_failed`             — fail-closed scrub artifacts retaining raw,
#                                    unredacted content (review already hides
#                                    them, so upload must too)
#
# Review-only artifacts (`.video_review.mp4`) are dot-prefixed, so the dotfile
# filter in `list_recording_files` already skips them — no entry needed here.
_RAW_DB_NAMES = frozenset({"recording.db", "recording.db-wal", "recording.db-shm"})
_RAW_SUFFIXES = (".scrub_failed",)


def _is_raw_artifact(rel_name: str) -> bool:
    """True if *rel_name* (a FULL relative path, forward-slashed) is a raw,
    never-upload artifact under the R8 local-only rule.

    Matched by basename of the full relative path, so a nested
    `nested/recording.db` is excluded just like a top-level one.
    """
    base = rel_name.rsplit("/", 1)[-1]
    if base in _RAW_DB_NAMES:
        return True
    return any(base.endswith(suffix) for suffix in _RAW_SUFFIXES)


def assert_uploadable(file_info: FileInfo) -> FileInfo:
    """Hard safety gate: raise if *file_info* names a raw, never-upload artifact.

    The R8 invariant ("the raw `recording.db` with unscrubbed PII is local-only
    and is NEVER uploaded") must FAIL LOUDLY when violated, not silently drop the
    file — a silent drop hides a programming error that could later regress into
    an actual leak. Returns *file_info* unchanged when it is uploadable, so call
    sites can wrap an enqueue inline.
    """
    if _is_raw_artifact(file_info.name):
        raise ValueError(
            f"refusing to upload raw local-only artifact {file_info.name!r}: "
            "recording.db (and its WAL/SHM sidecars and *.scrub_failed files) "
            "contain unscrubbed content and must never leave the machine (R8)."
        )
    return file_info


# ---------------------------------------------------------------------------
# SCR-126 / Fix 1: the masked-video upload-seam gate.
#
# Sibling of `assert_uploadable`. When the FROZEN `masked_video_upload` decision
# is ON for a recording, the post-hoc masker is the ONLY source of cloud chunk
# video — every cloud `chunk_NNNN.mp4` must be the masked copy materialized under
# `<name>-scrubbed/masked_video/` (`scrubber.masked_video_dir`), NEVER the rich
# capture-time source. Unlike the existing masked-path *switch* (which each upload
# caller applies), this gate sits at the enqueue boundary so a switch regression
# in ANY path (live `_collect_chunk_files`, terminal `_upload_source_chunk_media`,
# or the scrubbed-dir rglob `upload_recording`) fails loud instead of shipping a
# clear chunk. It is path-structural, not content-inspecting, by design: a clear
# copy can only legitimately live under `masked_video/` as a coverage-proven
# `UNMASKED_PROVABLY_SAFE` output, and a stale/foreign copy there is the
# scrubbed-copy reuse guard's concern (SCR-126 Fix 3), not this gate's.
#
# `masked_upload_on` is the FROZEN per-recording decision the CALLER resolves once
# (via `pipeline_chunk_ops.get_frozen_masked_video_upload`) and passes in — this
# keeps `upload.py` free of a `catalog`/`.recording_intent` dependency and uses the
# SAME resolution the masked-path switch uses, so the gate is a true backstop of
# the switch. The resolver's fallback-to-global (on a missing/legacy intent) only
# activates the gate when the global is ON — the fail-closed direction (it demands
# a masked path); with the flag OFF (today's default) the gate is inert and OFF /
# legacy recordings ship their capture-blocked source exactly as before.
_CHUNK_VIDEO_RE = re.compile(r"chunk_\d+\.mp4")
_MASKED_VIDEO_DIRNAME = "masked_video"


def assert_video_masked(file_info: FileInfo, *, masked_upload_on: bool) -> FileInfo:
    """Hard safety gate: refuse to enqueue an unmasked cloud chunk video.

    No-op when ``masked_upload_on`` is False (the OFF default — the capture-blocked
    source IS the cloud copy). When True, a ``chunk_NNNN.mp4`` whose local path is
    not under a ``masked_video/`` directory raises — the rich source must never
    leave the machine while masked upload is enabled (SCR-126 Fix 1, fail closed).
    Non-video artifacts pass through unchanged. Returns *file_info* when uploadable
    so call sites can wrap an enqueue inline.
    """
    if not masked_upload_on:
        return file_info
    base = file_info.name.rsplit("/", 1)[-1]
    if not _CHUNK_VIDEO_RE.fullmatch(base):
        return file_info
    if _MASKED_VIDEO_DIRNAME in file_info.path.parts:
        return file_info
    raise ValueError(
        f"refusing to upload unmasked cloud chunk video {file_info.name!r} "
        f"(path {file_info.path}): masked_video_upload is ON for this recording, "
        f"so every cloud chunk_*.mp4 must be the post-hoc-masked copy under "
        f"{_MASKED_VIDEO_DIRNAME}/ — never the rich capture-time source (SCR-126)."
    )


def list_recording_files(recording_dir: Path) -> list[FileInfo]:
    """Return files in a recording dir, sorted largest-first.

    Runs a WAL checkpoint on recording.db first to ensure a clean DB
    (a no-op when the DB is absent, e.g. legacy/migrated recordings — the
    checkpoint must not crash the upload), then enumerates every file EXCEPT
    the raw, never-upload artifacts defined by the R8 exclusion rule
    (`recording.db` + its `-wal`/`-shm` sidecars + `*.scrub_failed`), matched
    by full relative path so nested copies are excluded too.
    """
    _wal_checkpoint(recording_dir)

    files = []
    for p in sorted(recording_dir.iterdir()):
        if p.is_symlink() or not p.is_file() or p.name.startswith("."):
            continue
        if _is_raw_artifact(p.name):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append(FileInfo(
            name=p.name,
            path=p,
            content_type=_content_type(p),
            size=size,
        ))
    # Also include files in subdirectories (e.g., screenshots/)
    for p in sorted(recording_dir.rglob("*")):
        if p.is_symlink() or not p.is_file() or p.parent == recording_dir or p.name.startswith("."):
            continue
        rel = p.relative_to(recording_dir)
        # Match the exclusion against the FULL relative path so a nested
        # `recording.db` / sidecar / `*.scrub_failed` is excluded too.
        if _is_raw_artifact(rel.as_posix()):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append(FileInfo(
            name=rel.as_posix(),  # forward slashes on all platforms
            path=p,
            content_type=_content_type(p),
            size=size,
        ))
    files.sort(key=lambda f: f.size, reverse=True)
    return files


def request_signed_urls(
    recording_name: str, files: list[FileInfo],
) -> tuple[dict[str, str | None], str]:
    """POST to Cloud Function, return (urls_dict, gcs_prefix).

    urls_dict maps filename -> signed_url or None (already uploaded).

    Attaches a Firebase ``Authorization: Bearer`` token (the function is now the
    per-user security boundary). On a 401 — the server rejecting a token we still
    believe is valid (clock skew, rotation, revocation) — forces a refresh and
    retries the single call once. ``NotSignedIn`` is surfaced as a clear "sign in"
    message; a transient ``AuthError`` (refresh network failure / Firebase outage)
    propagates loudly. The live-upload path turns either into a failed chunk
    (``ChunkStatus.FAILED``) and never deletes local media — see
    ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``.

    The bearer/refresh/retry policy lives in :func:`screencap.auth.authed_post`;
    the ``requests.post`` reference is passed in so it stays this module's own call
    (test mocks at ``screencap.upload.requests.post`` keep working).
    """
    from screencap import auth

    url = _get_upload_url()
    payload = {
        "recording": recording_name,
        "files": [{"name": f.name, "content_type": f.content_type} for f in files],
    }
    try:
        resp = auth.authed_post(requests.post, url, json=payload, timeout=30)
    except auth.NotSignedIn:
        raise RuntimeError("Sign in to upload to the cloud: run `screencap login`.")
    except auth.AuthError as e:
        # The forced-refresh on the 401 retry hit a transient failure (refresh
        # network error / token-service outage). The credential is still valid —
        # surface a retryable message, not a forced re-login.
        raise RuntimeError(f"Cloud auth temporarily unavailable; try again: {e}")
    except requests.ConnectionError:
        raise RuntimeError("Upload service unavailable. Check your internet connection.")
    except requests.Timeout:
        raise RuntimeError("Upload service timed out. Try again later.")

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Upload service error: {detail}")

    data = resp.json()
    return data.get("urls", {}), data.get("gcs_prefix", "")


class _ProgressFile:
    """Wraps a file object to update a Rich progress bar on each read()."""

    def __init__(self, fh, progress: Progress, task_id, size: int):
        self._fh = fh
        self._progress = progress
        self._task_id = task_id
        self._size = size
        self._read_bytes = 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._fh.read(n)
        if chunk:
            self._read_bytes += len(chunk)
            self._progress.update(self._task_id, completed=self._read_bytes)
        return chunk

    def __len__(self) -> int:
        """requests uses len() to set Content-Length header."""
        return self._size


def upload_recording(
    recording_dir: Path,
    dry_run: bool = False,
    max_retries: int = 1,
    force: bool = False,
    jobs: int = 4,
    masked_video_upload: bool = False,
) -> UploadResult:
    """Upload all files in a recording directory.

    1. Enumerate files
    2. Request signed URLs (server tells us which are new vs existing)
    3. Upload new files with progress bars (parallel via ThreadPoolExecutor)
    4. Return summary

    Emits structured stderr lifecycle events (upload_started,
    upload_file_done, upload_finished, upload_failed) consumed by the
    SwiftUI review window's UploadController. See _stderr_events.py.

    Installs a SIGTERM handler so the SwiftUI shell can cancel an
    in-progress upload via subprocess.terminate(); restored on exit.
    """
    # Read immutable recording_id if available, fallback to dir name
    _id_file = recording_dir / ".recording_id"
    recording_name = _id_file.read_text().strip() if _id_file.exists() else recording_dir.name

    if not dry_run and not force and is_uploaded(recording_dir):
        console.print(
            f"  [dim]Already uploaded (use --force to re-upload)[/dim]"
        )
        return UploadResult(recording=recording_name)

    files = list_recording_files(recording_dir)

    if not files:
        raise FileNotFoundError(f"No files found in {recording_name}")

    # R8 defense-in-depth: re-assert at the upload seam that no raw local-only
    # artifact (recording.db + sidecars, *.scrub_failed) is in the set. The
    # list_recording_files denylist already excludes them, but routing the set
    # through assert_uploadable makes the "enforced two ways" guarantee real —
    # a future enqueue path that bypasses list_recording_files still hits this
    # gate, and it fails loud rather than silently shipping raw PII.
    for f in files:
        assert_uploadable(f)
        # SCR-126 Fix 1: when masked-video upload is ON, the scrubbed-dir rglob
        # (the path that ships masked_video/chunk_NNNN.mp4) must never enumerate a
        # rich-source chunk video. The caller resolves the frozen decision from the
        # SOURCE recording dir and passes it in (the scrubbed dir is what's
        # enumerated here, so its own intent is not authoritative).
        assert_video_masked(f, masked_upload_on=masked_video_upload)

    total_size = sum(f.size for f in files)
    console.print(
        f"\nUploading [bold]{recording_name}[/bold] "
        f"({_fmt_size(total_size)}, {len(files)} files)..."
    )

    if dry_run:
        for f in files:
            console.print(f"  {f.name:<40s} {_fmt_size(f.size):>10s}")
        console.print(f"\n[dim]Dry run — nothing uploaded.[/dim]")
        return UploadResult(recording=recording_name, total_bytes=total_size)

    # Install SIGTERM→KeyboardInterrupt so window-close-as-cancel from the
    # SwiftUI shell surfaces through the same path Ctrl+C already uses.
    # signal.signal requires the main thread; ValueError is raised on
    # background threads. Skip silently in that case — the CLI invocation
    # path is always main-thread, so cancel still works in practice.
    #
    # _UNSET sentinel disambiguates "install succeeded with None as the
    # previous handler" (legitimate per stdlib — C-set handlers report as
    # None) from "install never ran" (off-main-thread ValueError). Without
    # the sentinel, the finally restore would silently skip the C-set case
    # and leak our handler past the function. Installing INSIDE the outer
    # try ensures a signal arriving between install and try-entry still
    # routes through the finally restore.
    _UNSET: object = object()
    _previous_sigterm: object = _UNSET
    _failed_emitted = False
    try:
        try:
            _previous_sigterm = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        except ValueError:
            pass  # off-main-thread — restore guard below will no-op.

        emit_event(
            EVENT_UPLOAD_STARTED,
            recording=recording_name,
            file_count=len(files),
            total_bytes=total_size,
        )

        # Request signed URLs
        urls, gcs_prefix = request_signed_urls(recording_name, files)

        result = UploadResult(recording=recording_name, gcs_prefix=gcs_prefix)
        to_upload = []
        rejected = []
        for f in files:
            if f.name not in urls:
                # Server didn't return this filename at all (rejected by validation)
                rejected.append(f.name)
            elif urls[f.name] is None:
                result.skipped.append(f.name)
            else:
                to_upload.append((f, urls[f.name]))

        if rejected:
            console.print(
                f"  [yellow]Warning: {len(rejected)} file(s) rejected by server "
                f"(invalid filename)[/yellow]"
            )
            for name in rejected:
                console.print(f"    [dim]{name}[/dim]")
            result.failed.extend(rejected)

        if result.skipped:
            console.print(
                f"  [dim]Skipped {len(result.skipped)} file(s) (already uploaded)[/dim]"
            )

        if not to_upload:
            if result.failed:
                # Server rejected every file we offered (filename validation).
                # Emit upload_failed — emitting upload_finished with failed>0
                # is contradictory and would confuse the consumer state machine.
                # Do not write the upload_status.json sentinel: nothing landed.
                console.print(
                    f"  [yellow]No files uploaded — {len(result.failed)} rejected by server.[/yellow]"
                )
                emit_event(
                    EVENT_UPLOAD_FAILED,
                    recording=recording_name,
                    error=f"server rejected {len(result.failed)} file(s)",
                    uploaded=len(result.uploaded),
                    failed=len(result.failed),
                    total_bytes=result.total_bytes,
                )
                return result

            console.print(f"  [dim]All files already uploaded.[/dim]")
            # Emit upload_finished BEFORE _write_upload_status so an OSError
            # on the local sentinel write (disk full, RO recording dir)
            # doesn't flip a genuinely-successful run to upload_failed via
            # the catch-all below. Sentinel write is best-effort; the
            # cloud-side state is what matters for the consumer.
            emit_event(
                EVENT_UPLOAD_FINISHED,
                recording=recording_name,
                uploaded=len(result.uploaded),
                skipped=len(result.skipped),
                failed=len(result.failed),
                total_bytes=result.total_bytes,
                gcs_prefix=result.gcs_prefix,
            )
            try:
                _write_upload_status(recording_dir, result)
            except OSError as e:
                console.print(
                    f"  [yellow]warning: failed to write upload status sentinel: {e}[/yellow]"
                )
            return result

        # Build a lookup for file sizes (used when collecting results)
        file_sizes = {f.name: f.size for f, _ in to_upload}
        errors: list[tuple[str, str]] = []
        files_total = len(to_upload)

        # Upload with progress bars
        with Progress(
            TextColumn("  {task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            # 1. Create all progress tasks upfront
            task_ids = {}
            for f, _ in to_upload:
                task_ids[f.name] = progress.add_task(f.name, total=f.size)

            # 2. Submit all uploads
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = {}
                for f, signed_url in to_upload:
                    future = executor.submit(
                        _upload_with_progress,
                        f, signed_url, progress, task_ids[f.name],
                        recording_name, max_retries,
                    )
                    futures[future] = f.name

                # 3. Collect results as they complete
                try:
                    for future in as_completed(futures):
                        fname = futures[future]
                        try:
                            future.result()
                            result.uploaded.append(fname)
                            result.total_bytes += file_sizes[fname]
                            emit_event(
                                EVENT_UPLOAD_FILE_DONE,
                                recording=recording_name,
                                name=fname,
                                bytes_uploaded_so_far=result.total_bytes,
                                files_done=len(result.uploaded),
                                files_total=files_total,
                            )
                        except Exception as e:
                            progress.update(
                                task_ids[fname],
                                description=f"[red]{fname} (failed)[/red]",
                            )
                            result.failed.append(fname)
                            errors.append((fname, str(e)))
                except KeyboardInterrupt:
                    executor.shutdown(wait=False, cancel_futures=True)
                    console.print("\n[yellow]Upload interrupted.[/yellow]")
                    emit_event(
                        EVENT_UPLOAD_FAILED,
                        recording=recording_name,
                        error="interrupted",
                        uploaded=len(result.uploaded),
                        failed=len(result.failed),
                        total_bytes=result.total_bytes,
                    )
                    _failed_emitted = True
                    raise

        # 4. Print errors after progress block exits
        for filename, err in errors:
            console.print(f"  [red]Error uploading {filename}:[/red] {err}")

        if not result.failed:
            # Emit before write — see rationale on the early-return branch above.
            emit_event(
                EVENT_UPLOAD_FINISHED,
                recording=recording_name,
                uploaded=len(result.uploaded),
                skipped=len(result.skipped),
                failed=len(result.failed),
                total_bytes=result.total_bytes,
                gcs_prefix=result.gcs_prefix,
            )
            try:
                _write_upload_status(recording_dir, result)
            except OSError as e:
                console.print(
                    f"  [yellow]warning: failed to write upload status sentinel: {e}[/yellow]"
                )
        else:
            # Per-file failures landed in result.failed (errors list above).
            # The `error` field carries a compact summary; per-file detail
            # is in the preceding console output. KeyboardInterrupt has its
            # own emit site inside the executor block above.
            failure_summary = "; ".join(f"{n}: {e}" for n, e in errors[:3])
            if len(errors) > 3:
                failure_summary += f"; (+{len(errors) - 3} more)"
            emit_event(
                EVENT_UPLOAD_FAILED,
                recording=recording_name,
                error=failure_summary or "one or more files failed to upload",
                uploaded=len(result.uploaded),
                failed=len(result.failed),
                total_bytes=result.total_bytes,
            )

        return result

    except KeyboardInterrupt:
        # Guarantee exactly one terminal event on every cancel path.
        # The inner handler at the as_completed loop emits upload_failed
        # when cancel lands inside the executor block; this branch covers
        # cancel arriving before the executor (during emit_started,
        # request_signed_urls, the all-skipped early-return, or
        # _write_upload_status) so the SwiftUI consumer always observes
        # a terminal event.
        if not _failed_emitted:
            # `result` may not exist if cancel arrived before the
            # `result = UploadResult(...)` assignment at line 312.
            uploaded = len(result.uploaded) if "result" in locals() else 0
            failed = len(result.failed) if "result" in locals() else 0
            total_bytes = result.total_bytes if "result" in locals() else 0
            emit_event(
                EVENT_UPLOAD_FAILED,
                recording=recording_name,
                error="interrupted",
                uploaded=uploaded,
                failed=failed,
                total_bytes=total_bytes,
            )
        raise
    except Exception as e:
        # Catch-all for unexpected exceptions (RuntimeError from
        # request_signed_urls, FileNotFoundError edge cases, etc.) so the
        # SwiftUI shell always sees a terminal event before the process
        # exits. Re-raise so the existing CLI error-handling path
        # (cli/__init__.py upload command) still surfaces the error.
        emit_event(
            EVENT_UPLOAD_FAILED,
            recording=recording_name,
            error=str(e),
        )
        raise
    finally:
        # Restore only if install actually succeeded. _UNSET means we
        # never made it past the install attempt (off-main-thread). A
        # genuine None from signal.signal (C-set previous handler) still
        # triggers the restore — SIG_DFL is the safe default in that case.
        if _previous_sigterm is not _UNSET:
            try:
                signal.signal(
                    signal.SIGTERM,
                    _previous_sigterm if _previous_sigterm is not None else signal.SIG_DFL,
                )
            except ValueError:
                pass


def _upload_with_progress(
    f: FileInfo,
    signed_url: str,
    progress: Progress,
    task_id,
    recording_name: str,
    max_retries: int,
) -> None:
    """Upload a single file with streaming progress and retry on URL expiry."""
    for attempt in range(1 + max_retries):
        with open(f.path, "rb") as fh:
            progress.reset(task_id)
            wrapper = _ProgressFile(fh, progress, task_id, f.size)
            resp = requests.put(
                signed_url,
                data=wrapper,
                headers={
                    "Content-Type": f.content_type,
                    "Content-Length": str(f.size),
                },
                timeout=(10, 300),
            )

        if resp.status_code == 403 and attempt < max_retries:
            console.print(f"  [yellow]URL expired for {f.name}, retrying...[/yellow]")
            urls, _ = request_signed_urls(recording_name, [f])
            new_url = urls.get(f.name)
            if new_url:
                signed_url = new_url
                continue
            # Server returned None → file now exists (first attempt succeeded)
            return
        resp.raise_for_status()
        return


def resolve_recording_dirs(
    names: tuple[str, ...],
    all_recordings: bool = False,
) -> list[Path]:
    """Resolve recording names/flags to a list of recording directories."""
    recordings_dir = get_recordings_dir()

    if all_recordings:
        dirs = []
        for d in sorted(recordings_dir.iterdir()):
            if not d.is_dir():
                continue
            if (d / "recording.db").exists():
                dirs.append(d)
        if not dirs:
            raise FileNotFoundError("No recordings found.")
        return dirs

    if names:
        dirs = []
        for name in names:
            d = recordings_dir / name
            # Prevent path traversal: absolute paths or ".." segments
            try:
                d.resolve().relative_to(recordings_dir.resolve())
            except ValueError:
                raise FileNotFoundError(f"Recording not found: {name}")
            if not d.is_dir():
                raise FileNotFoundError(f"Recording not found: {name}")
            dirs.append(d)
        return dirs

    # Interactive selection
    from screencap.catalog import list_recordings

    recordings = list_recordings()
    if not recordings:
        raise FileNotFoundError("No recordings found.")

    console.print("\nAvailable recordings:")
    for i, r in enumerate(recordings, 1):
        console.print(f"  {i}. {r.name} ({r.size_mb})")

    import click

    choices = click.prompt(
        "Select recordings (comma-separated numbers, or 'all')",
        type=str,
    )

    if choices.strip().lower() == "all":
        return [recordings_dir / r.name for r in recordings]

    indices = []
    for part in choices.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(recordings):
                indices.append(idx - 1)
    if not indices:
        raise ValueError("No valid selections.")

    return [recordings_dir / recordings[i].name for i in indices]
