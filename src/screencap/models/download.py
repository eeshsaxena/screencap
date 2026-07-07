"""Download + integrity engine for the local model (U4, KTD5).

Fetches a manifest-pinned model from Hugging Face and verifies it before marking
it installed. Every safety property is fail-closed — a hash mismatch, a disallowed
(executable) format, an interrupted/cancelled download, or insufficient disk
aborts and cleans up **symlink-safe**, leaving no directory the U2 provider's path
resolver would treat as installed.

Layout: ``<models_dir>/<model_id>/<runtime>/`` holds the verified files plus an
``.installed`` marker written **last**. A download stages into a ``.partial``
sibling and is renamed into place only after full verification, so a crash never
leaves a half-populated "installed" dir.

The real Hugging Face fetch (:func:`_default_snapshot`) cannot run in CI; it is
injected (``snapshot_fn``) in tests and covered by manual runs. The verify /
perms / TOCTOU / fail-closed / precheck / idempotency logic below is CI-tested.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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

ProgressCb = Callable[[int, int], None]  # (bytes_done, bytes_total)


class ModelNotPinnedError(RuntimeError):
    """The variant's revision / file hashes are not release-pinned yet (KTD5)."""


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

    prev = os.environ.get("HF_ENDPOINT")
    os.environ["HF_ENDPOINT"] = _HF_ENDPOINT  # pin the host (ignore any override)
    try:
        snapshot_download(
            repo_id=repo,
            revision=revision,
            local_dir=str(local_dir),
            local_dir_use_symlinks=False,  # materialize real files (close TOCTOU)
            allow_patterns=filenames or None,
        )
    finally:
        if prev is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = prev


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

    staging = target.with_name(target.name + ".partial")
    _rmtree_safe(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(staging, 0o700)
    except OSError:
        pass

    total = variant.size_bytes
    if progress_cb:
        progress_cb(0, total)

    try:
        filenames = [f.name for f in variant.files]
        snapshot(variant.repo, variant.revision, staging, filenames)

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
    except Exception as exc:  # network / disk / snapshot error
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
