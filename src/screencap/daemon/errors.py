"""Daemon API error codes, envelopes, and exception wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from screencap.daemon.schema import envelope

LOCK_CONTENDED = "lock_contended"
NOT_OWNED_BY_DAEMON = "not_owned_by_daemon"
SCHEMA_MISMATCH = "schema_mismatch"
SLOW_CONSUMER = "slow_consumer"
CURSOR_UNKNOWN = "cursor_unknown"
CATALOG_UNREADABLE = "catalog_unreadable"
ROGUE_FILE = "rogue_file"
RECONCILING = "reconciling"
FORCE_MISMATCH = "force_mismatch"

# Codes returned by the daemon outside the typed-exception paths (route
# handler `except Exception`, query-string parse failures). Keeping them
# as named constants prevents drift between handlers and tests.
ERROR_CODE_INTERNAL = "internal_error"
ERROR_CODE_INVALID_CURSOR = "invalid_cursor"

EXCEPTION_TO_ERROR_CODE: dict[type["DaemonAPIError"], str] = {}


def error_envelope(*, schema_version: int, error: str, **payload: Any) -> dict[str, Any]:
    """Build a symmetric daemon error envelope ready for JSONResponse."""
    return envelope(schema_version=schema_version, ok=False, error=error, **payload)


def lock_contended_envelope(
    owner_payload: dict[str, Any],
    *,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=LOCK_CONTENDED,
        owner=owner_payload,
    )


def not_owned_by_daemon_envelope(
    *,
    claimant: str | None,
    schema_version: int,
    hint: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"claimant": claimant}
    if hint is not None:
        payload["hint"] = hint
    return error_envelope(schema_version=schema_version, error=NOT_OWNED_BY_DAEMON, **payload)


def schema_mismatch_envelope(
    *,
    expected: int,
    got: int | None,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=SCHEMA_MISMATCH,
        expected=expected,
        got=got,
    )


def slow_consumer_envelope(*, schema_version: int) -> dict[str, Any]:
    return error_envelope(schema_version=schema_version, error=SLOW_CONSUMER)


def cursor_unknown_envelope(
    *,
    requested_cursor: int,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=CURSOR_UNKNOWN,
        requested_cursor=requested_cursor,
    )


def catalog_unreadable_envelope(
    *,
    reason: str,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=CATALOG_UNREADABLE,
        reason=reason,
    )


def rogue_file_envelope(
    *,
    path: str | Path,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=ROGUE_FILE,
        path=str(path),
    )


def reconciling_envelope(*, schema_version: int) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=RECONCILING,
        hint="wait for reconciliation to complete",
    )


def force_mismatch_envelope(*, schema_version: int) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=FORCE_MISMATCH,
        hint="force=true expected_claimant_pid/expected_started_at did not match current lock owner",
    )


class DaemonAPIError(Exception):
    """Base class for errors route handlers can convert to JSON responses."""

    error_code: ClassVar[str]
    http_status: int = 500
    http_headers: ClassVar[dict[str, str]] = {}

    def __init__(self, *, schema_version: int, http_status: int | None = None) -> None:
        self.schema_version = schema_version
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.error_code)

    def envelope(self) -> dict[str, Any]:
        return error_envelope(schema_version=self.schema_version, error=self.error_code)

    def response_headers(self) -> dict[str, str]:
        """Per-error advisory headers (e.g. ``Retry-After``)."""
        return dict(self.http_headers)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        error_code = getattr(cls, "error_code", None)
        if isinstance(error_code, str):
            EXCEPTION_TO_ERROR_CODE[cls] = error_code


class LockContendedError(DaemonAPIError):
    error_code = LOCK_CONTENDED
    http_status = 409

    def __init__(
        self,
        owner: dict[str, Any],
        *,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.owner = owner
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return lock_contended_envelope(self.owner, schema_version=self.schema_version)


class NotOwnedByDaemonError(DaemonAPIError):
    error_code = NOT_OWNED_BY_DAEMON
    http_status = 409

    def __init__(
        self,
        claimant: str | None,
        *,
        schema_version: int,
        hint: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.claimant = claimant
        self.hint = hint
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return not_owned_by_daemon_envelope(
            claimant=self.claimant,
            schema_version=self.schema_version,
            hint=self.hint,
        )


class SchemaMismatchError(DaemonAPIError):
    error_code = SCHEMA_MISMATCH
    http_status = 400

    def __init__(
        self,
        *,
        expected: int,
        got: int | None,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.expected = expected
        self.got = got
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return schema_mismatch_envelope(
            expected=self.expected,
            got=self.got,
            schema_version=self.schema_version,
        )


class SlowConsumerError(DaemonAPIError):
    error_code = SLOW_CONSUMER
    http_status = 429

    def envelope(self) -> dict[str, Any]:
        return slow_consumer_envelope(schema_version=self.schema_version)


class CursorUnknownError(DaemonAPIError):
    error_code = CURSOR_UNKNOWN
    http_status = 410

    def __init__(
        self,
        requested_cursor: int,
        *,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.requested_cursor = requested_cursor
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return cursor_unknown_envelope(
            requested_cursor=self.requested_cursor,
            schema_version=self.schema_version,
        )


class CatalogUnreadableError(DaemonAPIError):
    error_code = CATALOG_UNREADABLE
    http_status = 500

    def __init__(
        self,
        reason: str,
        *,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.reason = reason
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return catalog_unreadable_envelope(
            reason=self.reason,
            schema_version=self.schema_version,
        )


class RogueFileError(DaemonAPIError):
    error_code = ROGUE_FILE
    http_status = 409

    def __init__(
        self,
        path: str | Path,
        *,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.path = path
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return rogue_file_envelope(path=self.path, schema_version=self.schema_version)


class ReconcilingError(DaemonAPIError):
    error_code = RECONCILING
    http_status = 503
    # 5s sits between the SwiftUI client's 10s request timeout and the
    # daemon's 30s reconcile grace; long enough that the client doesn't
    # spin a tight retry, short enough to avoid burning the entire timeout.
    http_headers: ClassVar[dict[str, str]] = {"Retry-After": "5"}

    def envelope(self) -> dict[str, Any]:
        return reconciling_envelope(schema_version=self.schema_version)


class ForceMismatchError(DaemonAPIError):
    error_code = FORCE_MISMATCH
    http_status = 409

    def envelope(self) -> dict[str, Any]:
        return force_mismatch_envelope(schema_version=self.schema_version)


__all__ = [
    "LOCK_CONTENDED",
    "NOT_OWNED_BY_DAEMON",
    "SCHEMA_MISMATCH",
    "SLOW_CONSUMER",
    "CURSOR_UNKNOWN",
    "CATALOG_UNREADABLE",
    "ROGUE_FILE",
    "RECONCILING",
    "FORCE_MISMATCH",
    "ERROR_CODE_INTERNAL",
    "ERROR_CODE_INVALID_CURSOR",
    "EXCEPTION_TO_ERROR_CODE",
    "error_envelope",
    "lock_contended_envelope",
    "not_owned_by_daemon_envelope",
    "schema_mismatch_envelope",
    "slow_consumer_envelope",
    "cursor_unknown_envelope",
    "catalog_unreadable_envelope",
    "rogue_file_envelope",
    "reconciling_envelope",
    "force_mismatch_envelope",
    "DaemonAPIError",
    "LockContendedError",
    "NotOwnedByDaemonError",
    "SchemaMismatchError",
    "SlowConsumerError",
    "CursorUnknownError",
    "CatalogUnreadableError",
    "RogueFileError",
    "ReconcilingError",
    "ForceMismatchError",
]
