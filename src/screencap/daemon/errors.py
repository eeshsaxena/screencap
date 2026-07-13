"""Daemon API error codes, envelopes, and exception wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from screencap.daemon.schema import envelope

LOCK_CONTENDED = "lock_contended"
NOT_OWNED_BY_DAEMON = "not_owned_by_daemon"
NOT_RECORDING = "not_recording"
# Editable titles (U3): recording.rename selector could not be resolved to any
# recording directory, and the target is currently recording (mid-recording
# rename is deferred to the HUD), respectively.
RECORDING_NOT_FOUND = "recording_not_found"
RECORDING_ACTIVE = "recording_active"
SCHEMA_MISMATCH = "schema_mismatch"
SLOW_CONSUMER = "slow_consumer"
CURSOR_UNKNOWN = "cursor_unknown"
CATALOG_UNREADABLE = "catalog_unreadable"
ROGUE_FILE = "rogue_file"
RECONCILING = "reconciling"
FORCE_MISMATCH = "force_mismatch"
INVALID_NAME = "invalid_name"
INVALID_OUTPUT_DIR = "invalid_output_dir"
PERMISSION_REQUIRED = "permission_required"
INVALID_PERMISSION = "invalid_permission"
INVALID_RANGE = "invalid_range"
# SCR (paid-only launch, U8/U9): the local paywall is enforced
# (``SCREENCAP_LOCAL_PAYWALL_ENFORCE``) and no unexpired entitlement lease grants a
# paid tier — recording-start and the recall/search verbs refuse. Distinct
# 402-equivalent code so the client maps it to an "upgrade" affordance rather than
# a generic failure.
SUBSCRIPTION_REQUIRED = "subscription_required"
# SCR-186: a request body that fails model validation (missing required field or
# an out-of-bounds value) — returned as a typed 400 rather than a generic 500.
INVALID_REQUEST = "invalid_request"

# SCR-228: storage-location migration. STORAGE_MIGRATION_FAILED carries a
# specific `reason` (a storage_migration.Reason code, or "recording_active") plus
# a human `message`. MIGRATION_IN_PROGRESS is the recording.start refusal while a
# migration holds the daemon.
STORAGE_MIGRATION_FAILED = "storage_migration_failed"
MIGRATION_IN_PROGRESS = "migration_in_progress"

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


def invalid_name_envelope(
    *,
    reason: str,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=INVALID_NAME,
        reason=reason,
    )


def invalid_output_dir_envelope(
    *,
    reason: str,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=INVALID_OUTPUT_DIR,
        reason=reason,
    )


def cursor_unknown_envelope(
    *,
    requested_cursor: int,
    schema_version: int,
    daemon_cursor: int | None = None,
    oldest_retained_cursor: int | None = None,
) -> dict[str, Any]:
    # daemon_cursor + oldest_retained_cursor let the caller resubscribe with a
    # valid in-window cursor (daemon_cursor) without a separate snapshot
    # round-trip. Both are optional so the envelope stays stable for callers
    # that have not yet wired through the bus state at the boundary.
    payload: dict[str, Any] = {"requested_cursor": requested_cursor}
    if daemon_cursor is not None:
        payload["daemon_cursor"] = daemon_cursor
    if oldest_retained_cursor is not None:
        payload["oldest_retained_cursor"] = oldest_retained_cursor
    return error_envelope(
        schema_version=schema_version,
        error=CURSOR_UNKNOWN,
        **payload,
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


def permission_required_envelope(
    *,
    missing: list[str],
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=PERMISSION_REQUIRED,
        missing=list(missing),
    )


def invalid_permission_envelope(
    *,
    reason: str,
    schema_version: int,
) -> dict[str, Any]:
    return error_envelope(
        schema_version=schema_version,
        error=INVALID_PERMISSION,
        reason=reason,
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


class NotRecordingError(DaemonAPIError):
    """No daemon-owned recording is in progress (SCR-218).

    Returned by ``recording.mute`` when there is no live engine to forward the
    command to — a mute that raced a stop, or a spurious call while idle.
    """

    error_code = NOT_RECORDING
    http_status = 409


class RecordingNotFoundError(DaemonAPIError):
    """The ``recording.rename`` selector matched no recording (U3).

    Raised when neither a recording's ``.recording_id`` nor its directory name
    equals the requested ``recording_id``. Maps to HTTP 404 so the client can
    distinguish a stale/unknown selector from a validation error or a conflict.
    """

    error_code = RECORDING_NOT_FOUND
    http_status = 404


class RecordingActiveError(DaemonAPIError):
    """A ``recording.rename`` targeted the currently-active recording (U3).

    Mid-recording rename is deferred to the HUD, so renaming the live recording
    is refused here. Maps to HTTP 409 (conflict with current state), mirroring
    :class:`NotRecordingError`.
    """

    error_code = RECORDING_ACTIVE
    http_status = 409


class StorageMigrationError(DaemonAPIError):
    """A storage-location migration was refused or failed (SCR-228).

    Carries a specific ``reason`` (a ``storage_migration.Reason`` code such as
    ``cross_volume`` / ``cloud_synced`` / ``target_not_empty``, or
    ``recording_active``) plus a human ``message`` so the CLI can print it and
    the macOS UI can map the code to copy.
    """

    error_code = STORAGE_MIGRATION_FAILED
    http_status = 409

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.reason = reason
        self.message = message
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return error_envelope(
            schema_version=self.schema_version,
            error=self.error_code,
            reason=self.reason,
            message=self.message,
        )


class MigrationInProgressError(DaemonAPIError):
    """A recording.start was refused because a storage migration is running."""

    error_code = MIGRATION_IN_PROGRESS
    http_status = 409


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


class InvalidRequestError(DaemonAPIError):
    """A well-formed-but-invalid request body → HTTP 400 ``invalid_request``.

    The typed counterpart to :func:`app._validation_error_response`: raising it
    lets a handler funnel every malformed-input exit (bad pydantic body, a
    zero-length / out-of-range task span, a split point outside the segment, a
    merge of fewer than two segments) through the SAME ``except DaemonAPIError``
    audit path the other typed errors use, so no validation exit skips the audit
    line. Produces the identical envelope ``_validation_error_response`` builds.
    """

    error_code = INVALID_REQUEST
    http_status = 400


class SlowConsumerError(DaemonAPIError):
    error_code = SLOW_CONSUMER
    http_status = 429

    def envelope(self) -> dict[str, Any]:
        return slow_consumer_envelope(schema_version=self.schema_version)


class InvalidNameError(DaemonAPIError):
    """Recording name rejected by the canonical validator.

    Raised by ``screencap.daemon._name_validation.validate_recording_name``
    and mapped to HTTP 400 with the ``invalid_name`` envelope. Carries
    a short human-readable ``reason`` so the caller can surface why
    the name was rejected.
    """

    error_code = INVALID_NAME
    http_status = 400

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
        return invalid_name_envelope(
            reason=self.reason,
            schema_version=self.schema_version,
        )


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


class PermissionRequiredError(DaemonAPIError):
    """A daemon-backed recording start lacks a required TCC grant (U6).

    Raised by the ``recording.start`` handler *before* the engine spawns, so
    the failure is a synchronous, typed result on the call rather than a
    200-OK-then-crash (the old behavior, where the worker emitted
    ``EVENT_STARTED`` before its own preflight, then exited via
    ``permission_lost``). ``missing`` lists the canonical permission strings
    the app maps via ``PrivacyPane.from(permissionString:)``. Maps to HTTP 403
    so it routes through ``_api_error_response`` (a typed 4xx), never the
    ``except Exception`` 500.
    """

    error_code = PERMISSION_REQUIRED
    http_status = 403

    def __init__(
        self,
        missing: list[str],
        *,
        schema_version: int,
        http_status: int | None = None,
    ) -> None:
        self.missing = list(missing)
        super().__init__(schema_version=schema_version, http_status=http_status)

    def envelope(self) -> dict[str, Any]:
        return permission_required_envelope(
            missing=self.missing,
            schema_version=self.schema_version,
        )


class InvalidPermissionError(DaemonAPIError):
    """The ``permission.request`` verb (U8) received an unknown permission.

    Raised by the ``permission.request`` handler when the requested
    ``permission`` is not one of the canonical three
    (``screen_recording`` / ``accessibility`` / ``input_monitoring``).
    Mirrors the ``validate_recording_name`` allowlist gate so an
    unexpected value never reaches the daemon-side registration / real-
    capture dispatch. Maps to HTTP 400 with the ``invalid_permission``
    envelope; carries a static ``reason`` (never echoes the raw value).
    """

    error_code = INVALID_PERMISSION
    http_status = 400

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
        return invalid_permission_envelope(
            reason=self.reason,
            schema_version=self.schema_version,
        )


class InvalidOutputDirError(DaemonAPIError):
    """Requested output_dir is outside the allowed recordings root(s).

    Raised by ``Supervisor._allocate_capture_dir`` when the caller
    supplies an ``output_dir`` that resolves outside every path in the
    daemon's output-dir allowlist. Maps to HTTP 400 with the
    ``invalid_output_dir`` envelope.
    """

    error_code = INVALID_OUTPUT_DIR
    http_status = 400

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
        return invalid_output_dir_envelope(
            reason=self.reason,
            schema_version=self.schema_version,
        )


class SubscriptionRequiredError(DaemonAPIError):
    """The local paywall is enforced and no active tier entitles the request (U8/U9).

    Raised by ``recording.start`` (before ``supervisor.spawn`` signals success —
    per the typed-error-before-spawn convention, mirroring
    :class:`PermissionRequiredError`) and by the five recall/search verbs
    (``content.search`` / ``transcript.search`` / ``timeline.query`` /
    ``frame.nearest`` / ``apps.list``) when ``SCREENCAP_LOCAL_PAYWALL_ENFORCE`` is on
    and the KTD-4 entitlement lease grants no unexpired paid tier. The gate is a
    no-op (never raised) with the flag off, so the default path is byte-identical.

    Maps to HTTP 402 (Payment Required) so it routes through
    ``_api_error_response`` as a typed 4xx — never the ``except Exception`` 500 —
    and the client can map the ``subscription_required`` code to an upgrade
    affordance. Browse + export verbs (``recording.list`` / ``timeline.day`` /
    ``tasks.list`` / ``auth.whoami``) are deliberately never gated: a lapsed user
    keeps their own local data (R8).
    """

    error_code = SUBSCRIPTION_REQUIRED
    http_status = 402


__all__ = [
    "LOCK_CONTENDED",
    "NOT_OWNED_BY_DAEMON",
    "NOT_RECORDING",
    "RECORDING_NOT_FOUND",
    "RECORDING_ACTIVE",
    "SCHEMA_MISMATCH",
    "SLOW_CONSUMER",
    "CURSOR_UNKNOWN",
    "CATALOG_UNREADABLE",
    "ROGUE_FILE",
    "RECONCILING",
    "FORCE_MISMATCH",
    "INVALID_NAME",
    "INVALID_OUTPUT_DIR",
    "PERMISSION_REQUIRED",
    "INVALID_PERMISSION",
    "INVALID_RANGE",
    "SUBSCRIPTION_REQUIRED",
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
    "invalid_name_envelope",
    "invalid_output_dir_envelope",
    "permission_required_envelope",
    "invalid_permission_envelope",
    "DaemonAPIError",
    "LockContendedError",
    "NotOwnedByDaemonError",
    "NotRecordingError",
    "RecordingNotFoundError",
    "RecordingActiveError",
    "SchemaMismatchError",
    "InvalidRequestError",
    "SlowConsumerError",
    "CursorUnknownError",
    "CatalogUnreadableError",
    "RogueFileError",
    "ReconcilingError",
    "ForceMismatchError",
    "InvalidNameError",
    "InvalidOutputDirError",
    "PermissionRequiredError",
    "InvalidPermissionError",
    "SubscriptionRequiredError",
]
