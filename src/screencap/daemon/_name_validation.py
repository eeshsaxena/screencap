"""Canonical recording-name validator (Phase 2 U2.3).

A recording name flows from caller input through the daemon's
``recording.start`` handler, into ``recorder.start_recording``, onto
the filesystem at ``~/.screencap/recordings/<name>/``, and into the
catalog DB. Path-traversal characters or names that look like
relative paths would let a hostile or buggy caller escape the
recordings root.

This module is the single gate. The validator is intentionally
strict: it rejects path separators (``/`` and ``\\``), ``..``
segments, leading ``.``, NUL bytes, and lengths > 255. Callers
should pass raw caller-supplied names through ``validate_recording_name``
once at the request boundary; downstream code can assume the name is
already sanitized.
"""

from __future__ import annotations

# 255 is the per-component name limit on every macOS-deployable
# filesystem (HFS+, APFS, ext4 over NFS, etc.). Going past it would
# fail on the filesystem layer with EINVAL/ENAMETOOLONG anyway, but
# we reject earlier so the error is uniform.
_MAX_NAME_LEN = 255


def _reason(name: object, message: str) -> str:
    """Build a human-readable reason without echoing the unsafe name.

    Echoing a forbidden name back into the error envelope would let a
    crafted name (e.g., containing terminal escape sequences) reach
    operator logs unchanged. Always describe the violation in static
    text.
    """
    if isinstance(name, str):
        # Just the length is safe to echo; the bytes themselves are not.
        return f"{message} (length={len(name)})"
    return f"{message} (got non-string)"


def validate_recording_name(name: object) -> str:
    """Validate ``name`` for use as a recording directory + catalog key.

    Returns the validated name (a ``str``) on success. Raises
    ``InvalidNameError`` from the daemon errors module on rejection.

    Rules:
    - Must be a non-empty string.
    - Length must be 1..255 (inclusive).
    - Must not contain path separators (``/``, ``\\``).
    - Must not contain NUL.
    - Must not be ``.`` or ``..`` or contain any ``..`` segment.
    - Must not begin with ``.`` (no hidden-directory traversal).
    - Must not begin with a control character (< 0x20).
    """
    # Import locally to keep this module free of the broader daemon
    # error surface for non-daemon callers (e.g., direct
    # recorder.start_recording usage).
    from screencap.daemon import errors, schema

    schema_version = schema._RECORDING_START_API_VERSION

    if not isinstance(name, str):
        raise errors.InvalidNameError(
            _reason(name, "name must be a string"),
            schema_version=schema_version,
        )
    if not name:
        raise errors.InvalidNameError(
            "name must not be empty",
            schema_version=schema_version,
        )
    if len(name) > _MAX_NAME_LEN:
        raise errors.InvalidNameError(
            _reason(name, f"name exceeds {_MAX_NAME_LEN}-character limit"),
            schema_version=schema_version,
        )
    if "/" in name or "\\" in name:
        raise errors.InvalidNameError(
            "name must not contain path separators",
            schema_version=schema_version,
        )
    if "\x00" in name:
        raise errors.InvalidNameError(
            "name must not contain NUL bytes",
            schema_version=schema_version,
        )
    if name in (".", ".."):
        raise errors.InvalidNameError(
            "name must not be . or ..",
            schema_version=schema_version,
        )
    if ".." in name.split("/"):
        # Defensive — the path-separator check above already rejects /,
        # but if a future change loosens that, the .. component check
        # keeps the traversal gate.
        raise errors.InvalidNameError(
            "name must not contain .. segments",
            schema_version=schema_version,
        )
    if name.startswith("."):
        raise errors.InvalidNameError(
            "name must not begin with .",
            schema_version=schema_version,
        )
    if ord(name[0]) < 0x20:
        raise errors.InvalidNameError(
            "name must not begin with a control character",
            schema_version=schema_version,
        )
    return name


__all__ = ["validate_recording_name"]
