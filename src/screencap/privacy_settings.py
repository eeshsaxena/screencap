"""Privacy/settings config mutation: TOML writers, first-run prompts, settings
rendering, matrix validation, and the ``settings privacy`` mutation engine.

Extracted from the CLI (SCR-156, continuing SCR-32) so the privacy-config
read/modify/write helpers and the matrix-invariant validation seam are
unit-testable without a CliRunner round trip. Heavy imports (tomlkit, fcntl,
screencap.config, screencap.setup_wizard, screencap.privacy.*) stay deferred
inside function bodies to keep importing this module — and ``screencap --help``
— cheap. The ``settings privacy`` Click command itself stays in the CLI and
imports these helpers locally at its call sites.
"""

from __future__ import annotations

import contextlib
import logging
import os

from rich.console import Console

console = Console()
logger = logging.getLogger(__name__)


def _config_lock_path():
    """Return the path of the advisory config lock (sibling of recording.lock).

    Lazy resolution so test fixtures that monkey-patch ``_DEFAULT_BASE``
    pick up the right path each call.
    """
    from screencap.config import _DEFAULT_BASE
    return _DEFAULT_BASE / "run" / "config.lock"


_PRIVACY_CONFIG_FLOCK_TIMEOUT_S = 5.0


class PrivacyConfigLockTimeout(RuntimeError):
    """Raised when the advisory flock on config.lock can't be acquired in
    ``_PRIVACY_CONFIG_FLOCK_TIMEOUT_S``. Surfaces a stuck holder (e.g., a
    crashed peer on NFS / sshfs) so ``screencap start`` and
    ``screencap settings privacy`` fail fast instead of hanging."""


@contextlib.contextmanager
def _privacy_config_writer():
    """Read-modify-write the privacy config under an advisory flock (todo 025).

    Holds an exclusive flock on ``~/.screencap/run/config.lock`` for the
    duration of the read → mutate → save cycle. Without this, two concurrent
    ``screencap settings privacy`` invocations race on read-modify-write and
    silently drop one of the writes (todo 015). ``_save_config_atomic``
    provides write-atomicity, not lost-update protection — the flock does.

    The acquire is non-blocking with a bounded retry loop (todo 006). A
    blocking ``flock(LOCK_EX)`` could hang ``screencap start`` indefinitely
    if a stale holder kept the lock — Python's ``fcntl.flock`` only raises
    on signal interruption. We poll every 100ms up to
    ``_PRIVACY_CONFIG_FLOCK_TIMEOUT_S``; on timeout we raise
    ``PrivacyConfigLockTimeout`` so the caller can surface a clear error
    rather than stalling silently.

    Yields the tomlkit doc. The caller mutates in-place; on context exit
    (without exception) the doc is atomically saved and the config cache is
    invalidated. On exception the file is left untouched.
    """
    import fcntl as _fcntl
    import time as _time

    from screencap.config import _CONFIG_PATH, invalidate_config_cache
    from screencap.setup_wizard import _load_config_toml, _save_config_atomic

    lock_path = _config_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = _time.monotonic() + _PRIVACY_CONFIG_FLOCK_TIMEOUT_S
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _time.monotonic() >= deadline:
                    raise PrivacyConfigLockTimeout(
                        f"Could not acquire {lock_path} within "
                        f"{_PRIVACY_CONFIG_FLOCK_TIMEOUT_S:.0f}s — another "
                        f"screencap process may be holding the lock or have "
                        f"exited without releasing it. Re-run after the other "
                        f"process completes, or remove {lock_path} if no "
                        f"screencap process is active."
                    )
                _time.sleep(0.1)
        doc = _load_config_toml(_CONFIG_PATH)
        yield doc
        # Only reached on the no-exception path.
        _save_config_atomic(_CONFIG_PATH, doc)
        invalidate_config_cache()
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _write_privacy_flag(key: str, value: object) -> None:
    """Write a single [privacy] scalar via tomlkit, preserving comments and order.

    Used by the matrix-acknowledgement flow and by setup-skip — both need to
    set a single bool without disturbing other [privacy] keys (R16 invariant).
    """
    import tomlkit

    with _privacy_config_writer() as doc:
        if "privacy" not in doc:
            doc.add("privacy", tomlkit.table())
        doc["privacy"][key] = value
