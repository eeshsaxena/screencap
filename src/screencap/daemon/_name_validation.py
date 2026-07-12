"""Canonical recording-name validator (Phase 2 U2.3).

A recording name flows from caller input through the daemon's
``recording.start`` handler, into ``recorder.start_recording``, onto
the filesystem at ``~/.screencap/recordings/<name>/``, and into the
catalog DB. Path-traversal characters or names that look like
relative paths would let a hostile or buggy caller escape the
recordings root.

This module is the single gate. The validator uses a strict ASCII
safelist — only ``[A-Za-z0-9._ -]`` are permitted. This rejects all
Unicode letters, control characters, path separators, NUL bytes, and
shell-special characters in one pass, giving a minimal attack surface
for names that appear in log lines, directory paths, and catalog keys.
Additional structural rules (``..`` segments, leading ``.``, whitespace-
only, trailing dot, length > 255) are enforced on top of the safelist.

Callers should pass raw caller-supplied names through
``validate_recording_name`` once at the request boundary; downstream
code can assume the name is already sanitized.
"""

from __future__ import annotations

import re
import unicodedata

# Editable-title display-text cap (U3). A generous ceiling for a human label —
# long enough for a descriptive rename, short enough that it cannot be abused as
# a large payload smuggled through a "title". Counted in Unicode code points.
_MAX_TITLE_LEN = 200

# 255 is the per-component name limit on every macOS-deployable
# filesystem (HFS+, APFS, ext4 over NFS, etc.). Going past it would
# fail on the filesystem layer with EINVAL/ENAMETOOLONG anyway, but
# we reject earlier so the error is uniform.
_MAX_NAME_LEN = 255

# Strict ASCII safelist: letters, digits, dot, underscore, space, hyphen.
# Anything outside this set is rejected — including Unicode letters,
# control characters, shell metacharacters, ANSI escapes, and path separators.
_SAFE_CHARS_RE = re.compile(r"^[A-Za-z0-9._ -]+$")


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
    - Characters must be in the ASCII safelist: ``[A-Za-z0-9._ -]``.
    - Must not be whitespace-only.
    - Must not be ``.`` or ``..`` or contain any ``..`` segment.
    - Must not begin with ``.`` (no hidden-directory traversal).
    - Must not end with ``.`` (avoids Windows-style extension ambiguity).
    """
    # Import locally to keep this module free of the broader daemon
    # error surface for non-daemon callers (e.g., direct
    # recorder.start_recording usage).
    from screencap.daemon import errors, schema

    schema_version = schema.RECORDING_START_API_VERSION

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
    # ASCII safelist check covers: path separators, NUL, control chars,
    # Unicode letters, shell metacharacters, ANSI escape sequences in one pass.
    if not _SAFE_CHARS_RE.match(name):
        raise errors.InvalidNameError(
            "name must contain only ASCII letters, digits, dots, underscores, spaces, or hyphens",
            schema_version=schema_version,
        )
    if name.strip() == "":
        raise errors.InvalidNameError(
            "name must not be whitespace-only",
            schema_version=schema_version,
        )
    if name in (".", ".."):
        raise errors.InvalidNameError(
            "name must not be . or ..",
            schema_version=schema_version,
        )
    if ".." in name.split("/"):
        # Defensive — the safelist rejects / already, but keep this guard
        # in case a future change loosens the safelist.
        raise errors.InvalidNameError(
            "name must not contain .. segments",
            schema_version=schema_version,
        )
    if name.startswith("."):
        raise errors.InvalidNameError(
            "name must not begin with .",
            schema_version=schema_version,
        )
    if name.endswith("."):
        raise errors.InvalidNameError(
            "name must not end with .",
            schema_version=schema_version,
        )
    return name


def validate_recording_title(title: object) -> str:
    """Validate ``title`` as a recording's editable DISPLAY text (U3).

    This is deliberately NOT the path-safe :func:`validate_recording_name`. A
    title never becomes a directory, filename, or catalog key — it is stored in
    the local-only ``recording.db`` and shown in the UI — so unicode letters,
    emoji, spaces, and punctuation are all allowed. The gate is narrow:

    - Must be a string.
    - An EMPTY string is VALID and means "clear the rename → revert to the
      derived default"; it is returned unchanged.
    - Length must be at most 200 code points.
    - Must not contain any control character (Unicode category ``Cc``), which
      covers NUL, tabs, newlines, and ANSI escape introducers — the bytes that
      would corrupt a log line or terminal if a crafted title were echoed.

    Returns the validated title (unchanged) on success. Raises the same
    ``InvalidNameError`` :func:`validate_recording_name` raises on rejection,
    with a title-appropriate reason that NEVER echoes the raw title (only its
    length), for the identical operator-log-safety reason.
    """
    from screencap.daemon import errors, schema

    schema_version = schema._RECORDING_RENAME_API_VERSION

    if not isinstance(title, str):
        raise errors.InvalidNameError(
            _reason(title, "title must be a string"),
            schema_version=schema_version,
        )
    # Empty means "clear" — a valid request, returned as-is.
    if len(title) > _MAX_TITLE_LEN:
        raise errors.InvalidNameError(
            _reason(title, f"title exceeds {_MAX_TITLE_LEN}-character limit"),
            schema_version=schema_version,
        )
    # Reject every control character (category Cc) in one pass — NUL, C0/C1
    # controls, and the ESC that starts an ANSI sequence are all Cc.
    if any(unicodedata.category(ch) == "Cc" for ch in title):
        raise errors.InvalidNameError(
            "title must not contain control characters",
            schema_version=schema_version,
        )
    return title


__all__ = ["validate_recording_name", "validate_recording_title"]
