"""Cross-session persistence for runtime privacy decisions.

When the user clicks "Always disable" on the first-seen prompt during a
recording, this module appends the bundle ID (or root domain) to the
appropriate field of ``[privacy]`` in ``~/.screencap/config.toml`` so the
decision survives across recording sessions.

Cross-process safety: takes ``fcntl.flock(LOCK_EX)`` on
``~/.screencap/config.lock`` for the entire read-modify-write window so
parallel ``screencap setup`` invocations and prompt-driven persists
cannot clobber each other.

The current recording's ``RecorderPrivacyFilter`` does NOT need to
re-read config.toml — it picks up the user's intent immediately via the
existing ``override_q`` IPC. This module is purely about persistence
for the *next* recording.
"""

from __future__ import annotations

import fcntl
import logging
from pathlib import Path

import tomlkit

from screencap.config import (
    invalidate_config_cache,
    save_config_atomic,
)
from screencap.privacy.domain_loader import extract_root_domain

logger = logging.getLogger(__name__)

_BASE_DIR = Path.home() / ".screencap"
_CONFIG_PATH = _BASE_DIR / "config.toml"
_LOCK_PATH = _BASE_DIR / "config.lock"


def _load_or_create_doc(config_path: Path) -> tomlkit.TOMLDocument:
    """Load existing config.toml or return an empty document."""
    if config_path.exists():
        return tomlkit.parse(config_path.read_text())
    return tomlkit.document()


def _ensure_privacy_table(doc: tomlkit.TOMLDocument):
    """Ensure ``[privacy]`` exists in the document and return it."""
    if "privacy" not in doc:
        doc.add("privacy", tomlkit.table())
    return doc["privacy"]


def _append_unique(privacy, key: str, value: str) -> bool:
    """Append ``value`` to ``privacy[key]`` (a list) if not already present.

    Returns True if the value was added, False if it was already present.
    Creates the key as a tomlkit array if it doesn't exist.
    """
    if key not in privacy:
        arr = tomlkit.array()
        arr.append(value)
        privacy[key] = arr
        return True

    existing = privacy[key]
    # tomlkit arrays support __contains__ and iteration
    for item in existing:
        if str(item) == value:
            return False
    existing.append(value)
    return True


def persist_disable(
    bundle_id: str,
    domain: str | None,
    config_path: Path | None = None,
) -> bool:
    """Append a "disable" decision to the user's config.toml.

    - bundle_id only → adds to ``[privacy].exclude_apps``
    - bundle_id + domain → adds ``extract_root_domain(domain)`` to
      ``[privacy].mask_domains``

    Idempotent: silently no-ops if the entry is already present.
    Atomic: tempfile + os.replace via :func:`config.save_config_atomic`.
    Cross-process safe: holds an exclusive ``fcntl.flock`` on
    ``~/.screencap/config.lock`` for the read-modify-write window.

    On success, calls :func:`config.invalidate_config_cache` so the next
    recording's ``RecorderPrivacyFilter`` picks up the change.

    Args:
        bundle_id: macOS bundle identifier of the app (e.g.
            ``"com.tinyspeck.slackmacgap"``). Required, must be non-empty.
        domain: Browser domain if this is a browser tab, else ``None``.
            Will be collapsed to its registrable root via
            :func:`extract_root_domain` before being written.
        config_path: Override the default config path (for tests).

    Returns:
        ``True`` if the entry was added, ``False`` if it was already
        present or if the write failed.
    """
    if not bundle_id:
        return False

    cfg_path = config_path if config_path is not None else _CONFIG_PATH
    lock_path = cfg_path.parent / "config.lock"

    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("persist_disable: could not create config dir: %s", exc)
        return False

    # Open the lockfile (create if missing). The fd stays open for the
    # full read-modify-write window so flock holds across the rename.
    try:
        lock_fd = open(lock_path, "a+")
    except OSError as exc:
        logger.warning("persist_disable: could not open lockfile: %s", exc)
        return False

    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            logger.warning("persist_disable: flock failed: %s", exc)
            return False

        try:
            doc = _load_or_create_doc(cfg_path)
            privacy = _ensure_privacy_table(doc)

            if domain:
                root = extract_root_domain(domain)
                added = _append_unique(privacy, "mask_domains", root)
            else:
                added = _append_unique(privacy, "exclude_apps", bundle_id)

            if not added:
                return False

            save_config_atomic(cfg_path, doc)
            invalidate_config_cache()
            return True
        except Exception as exc:
            logger.warning("persist_disable: write failed: %s", exc)
            return False
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            lock_fd.close()
        except OSError:
            pass
