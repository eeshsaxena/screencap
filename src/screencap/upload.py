"""Upload recordings to GCS via signed URLs from a Cloud Function."""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
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

from screencap.config import get_recordings_dir

console = Console()

DEFAULT_UPLOAD_URL = (
    "https://screencap-recording-signed-url-397234807794.southamerica-east1.run.app"
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


def list_recording_files(recording_dir: Path) -> list[FileInfo]:
    """Return files in a recording dir, sorted largest-first."""
    files = []
    for p in sorted(recording_dir.iterdir()):
        if p.is_symlink() or not p.is_file():
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
        if p.is_symlink() or not p.is_file() or p.parent == recording_dir:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        rel = p.relative_to(recording_dir)
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
    """
    url = _get_upload_url()
    payload = {
        "recording": recording_name,
        "files": [{"name": f.name, "content_type": f.content_type} for f in files],
    }
    try:
        resp = requests.post(url, json=payload, timeout=30)
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
) -> UploadResult:
    """Upload all files in a recording directory.

    1. Enumerate files
    2. Request signed URLs (server tells us which are new vs existing)
    3. Upload new files with progress bars
    4. Return summary
    """
    recording_name = recording_dir.name
    files = list_recording_files(recording_dir)

    if not files:
        raise FileNotFoundError(f"No files found in {recording_name}")

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
        console.print(f"  [dim]All files already uploaded.[/dim]")
        return result

    # Upload with progress bars
    with Progress(
        TextColumn("  {task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    ) as progress:
        for f, signed_url in to_upload:
            task_id = progress.add_task(f.name, total=f.size)
            try:
                _upload_with_progress(
                    f, signed_url, progress, task_id,
                    recording_name, max_retries,
                )
                result.uploaded.append(f.name)
                result.total_bytes += f.size
            except Exception as e:
                progress.update(task_id, description=f"[red]{f.name} (failed)[/red]")
                result.failed.append(f.name)
                console.print(f"  [red]Error uploading {f.name}:[/red] {e}")

    return result


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
            if not d.is_dir() or d.name.endswith("-scrubbed"):
                continue
            if any((d / db).exists() for db in ("recording.db", "capture.db")):
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
