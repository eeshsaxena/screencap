"""Download recordings from GCS via signed URLs from a Cloud Function."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

from screencap.config import get_downloads_dir

console = Console()

DOWNLOAD_STATUS_FILE = ".download_status.json"

DEFAULT_DOWNLOAD_URL = (
    "https://get-upload-urls-wyldgq6aqa-rj.a.run.app"
)


def _get_download_url() -> str:
    return os.environ.get("SCREENCAP_DOWNLOAD_URL", DEFAULT_DOWNLOAD_URL)


def _fmt_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    if nbytes < 1024 * 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    return f"{nbytes / (1024 * 1024 * 1024):.1f} GB"


@dataclass
class RemoteRecording:
    name: str
    file_count: int
    total_size: int


@dataclass
class DownloadResult:
    recording: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    total_bytes: int = 0
    gcs_prefix: str = ""


def is_downloaded(recording_dir: Path) -> bool:
    """Check if a recording has already been downloaded."""
    return (recording_dir / DOWNLOAD_STATUS_FILE).is_file()


def _write_download_status(recording_dir: Path, result: DownloadResult) -> None:
    """Write download status marker to recording directory."""
    status = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "gcs_prefix": result.gcs_prefix,
        "files_downloaded": len(result.downloaded),
        "total_bytes": result.total_bytes,
    }
    (recording_dir / DOWNLOAD_STATUS_FILE).write_text(
        json.dumps(status, indent=2)
    )


def _resolve_dest_dir(dest: str | None) -> Path:
    """Resolve destination directory, defaulting to get_downloads_dir()."""
    if dest:
        p = Path(dest).expanduser().resolve()
    else:
        p = get_downloads_dir()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"Cannot create destination directory: {e}")
    return p


def list_remote_recordings() -> list[RemoteRecording]:
    """Fetch list of available recordings from the Cloud Function."""
    url = _get_download_url()
    try:
        resp = requests.post(url, json={"action": "list"}, timeout=60)
    except requests.ConnectionError:
        raise RuntimeError(
            "Download service unavailable. Check your internet connection."
        )
    except requests.Timeout:
        raise RuntimeError("Download service timed out. Try again later.")

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Download service error: {detail}")

    data = resp.json()
    return [
        RemoteRecording(
            name=r["name"],
            file_count=r["file_count"],
            total_size=r["total_size"],
        )
        for r in data.get("recordings", [])
    ]


def request_signed_urls(recording_name: str) -> tuple[dict[str, str], str]:
    """Request signed download URLs for a recording.

    Returns (urls_dict, gcs_prefix) where urls_dict maps filename -> signed URL.
    """
    url = _get_download_url()
    try:
        resp = requests.post(
            url,
            json={"action": "sign-download", "recording": recording_name},
            timeout=60,
        )
    except requests.ConnectionError:
        raise RuntimeError(
            "Download service unavailable. Check your internet connection."
        )
    except requests.Timeout:
        raise RuntimeError("Download service timed out. Try again later.")

    if resp.status_code == 404:
        detail = ""
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise FileNotFoundError(detail)

    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Download service error: {detail}")

    data = resp.json()
    return data.get("urls", {}), data.get("gcs_prefix", "")


def _download_file_with_progress(
    url: str, dest_path: Path, progress: Progress, task_id
) -> int:
    """Stream-download a file with progress updates. Returns bytes written."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    resp = requests.get(url, stream=True, timeout=(10, None))
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    if total:
        progress.update(task_id, total=total)

    written = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            written += len(chunk)
            progress.update(task_id, completed=written)

    return written


def download_recording(
    name: str,
    dest: Path,
    dry_run: bool = False,
    force: bool = False,
    jobs: int = 4,
) -> DownloadResult:
    """Download all files for a single recording.

    1. Check marker (skip if already downloaded, unless force)
    2. Request signed URLs
    3. Download files with progress (parallel via ThreadPoolExecutor)
    4. Write marker on success
    """
    recording_dir = dest / name
    result = DownloadResult(recording=name)

    if not dry_run and not force and is_downloaded(recording_dir):
        console.print(
            f"  [dim]Already downloaded (use --force to re-download)[/dim]"
        )
        return result

    # Get signed URLs (just before download to avoid expiry)
    urls, gcs_prefix = request_signed_urls(name)
    result.gcs_prefix = gcs_prefix

    if not urls:
        console.print(f"  [dim]No files found for {name}.[/dim]")
        return result

    total_files = len(urls)
    console.print(
        f"\nDownloading [bold]{name}[/bold] ({total_files} files)..."
    )

    if dry_run:
        for filename in sorted(urls):
            console.print(f"  {filename}")
        console.print(f"\n[dim]Dry run — nothing downloaded.[/dim]")
        return result

    recording_dir.mkdir(parents=True, exist_ok=True)
    errors: list[tuple[str, str]] = []

    with Progress(
        TextColumn("  {task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    ) as progress:
        # 1. Create all progress tasks upfront
        task_ids = {}
        for filename in sorted(urls):
            task_ids[filename] = progress.add_task(filename, total=0)

        # 2. Submit all downloads
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {}
            for filename, signed_url in sorted(urls.items()):
                dest_path = recording_dir / filename
                future = executor.submit(
                    _download_file_with_progress,
                    signed_url, dest_path, progress, task_ids[filename],
                )
                futures[future] = filename

            # 3. Collect results as they complete
            try:
                for future in as_completed(futures):
                    filename = futures[future]
                    try:
                        nbytes = future.result()
                        result.downloaded.append(filename)
                        result.total_bytes += nbytes
                    except Exception as e:
                        progress.update(
                            task_ids[filename],
                            description=f"[red]{filename} (failed)[/red]",
                        )
                        result.failed.append(filename)
                        errors.append((filename, str(e)))
            except KeyboardInterrupt:
                executor.shutdown(wait=False, cancel_futures=True)
                console.print("\n[yellow]Download interrupted.[/yellow]")
                raise

    # 4. Print errors after progress block exits
    for filename, err in errors:
        console.print(f"  [red]Error downloading {filename}:[/red] {err}")

    if not result.failed:
        _write_download_status(recording_dir, result)

    return result
