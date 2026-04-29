"""V1.5 NetworkScrubPipeline — decrypt + scrub at export.

Single integration point for the row-to-export-event transform when
network bodies are encrypted. KEK is loaded once at construction; DEK
is unwrapped once. ``decrypt_and_scrub(capture_event)`` runs
``DetectionPipeline`` over the decrypted plaintext and produces an
export-side Pydantic event with ``body_text`` set (scrubbed plaintext)
-- capture-side ``body_ciphertext`` fields never reach the export-side
event by construction.

Per-export-session lifecycle:

1. Caller invokes :class:`NetworkScrubPipeline` with the recording's
   ``db_path`` and ``recording_id``.
2. ``__init__`` loads the meta row from ``network_event_meta`` for this
   recording, fetches the KEK from Keychain, unwraps the DEK once, and
   builds a :class:`screencap.privacy.DetectionPipeline`. KEK plaintext
   is dropped after the unwrap; DEK plaintext is held for the lifetime
   of this pipeline instance.
3. ``decrypt_and_scrub(capture_event)`` is called per network event row
   that has ``body_ciphertext``. Returns the export-side event.
4. Caller discards the pipeline at end of export; DEK plaintext exits
   scope at process termination.

Failure semantics:

* KEK missing: callers route via :class:`KekUnavailableError`. The
  caller decides fail-loud vs fail-soft per the ticket's
  "KEK-missing semantics" block. Explicit ``screencap export`` is
  fail-loud (per the locked decision).
* Meta row missing: V1-vintage recording (no encryption); caller
  should detect this BEFORE constructing the pipeline by checking
  :func:`recording_has_encrypted_bodies`.
* Decryption fails (``InvalidTag``): per-row error; the event is
  yielded with ``body_text=None`` and a debug log entry. The
  ``body_aad IS NULL AND body_ciphertext IS NOT NULL`` integrity check
  runs BEFORE invoking ``AESGCM.decrypt`` to distinguish missing-AAD
  from genuine tampering (both produce ``InvalidTag`` post-decrypt).

Plan reference: ``docs/plans/2026-04-25-003-feat-network-proxy-logging-plan.md``
Unit 6 (V1.5 portion).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class KekUnavailableError(Exception):
    """KEK not present in Keychain (or otherwise unreachable).

    Distinct from per-row decryption failure -- raised at pipeline
    construction time when the long-lived KEK cannot be retrieved or the
    per-recording wrapped DEK cannot be unwrapped. Callers decide
    fail-loud vs fail-soft. Explicit ``screencap export`` is fail-loud
    per the locked decision in the V1.5 ticket.
    """


class NetworkScrubPipeline:
    """Decrypts + scrubs encrypted body ciphertext at export time.

    Holds the unwrapped DEK + a constructed ``DetectionPipeline`` for
    the lifetime of a single export run. Construct ONCE per export
    invocation, then call :meth:`decrypt_and_scrub` per encrypted
    network event row. The pipeline is bound to a single recording (the
    DEK is per-recording); reusing it across recordings would mix DEKs
    and silently produce ``InvalidTag`` for every row of the wrong
    recording.

    Threading: instances are NOT thread-safe. The DetectionPipeline's
    NLP backends rely on per-thread state, and there is no benefit to
    cross-thread sharing in the current export caller (single-threaded
    ``screencap export``).
    """

    def __init__(self, db_path: str, recording_id: int) -> None:
        """Build a per-recording decrypt+scrub pipeline.

        Args:
            db_path: Path to the recording's ``recording.db`` file.
            recording_id: ID of the recording. Used to look up the
                ``network_event_meta`` row containing the wrapped DEK.

        Raises:
            KekUnavailableError: when the meta row is absent (V1-vintage
                or non-network recording), the Keychain lookup fails
                (user cancelled the dialog, locked Keychain, no backend),
                or the DEK unwrap fails (``InvalidTag`` -- either the
                ciphertext is corrupt or the KEK has rotated and the
                wrapped DEK no longer matches).
        """
        # Lazy imports keep this module importable without the heavy
        # deps (cryptography / keyring / presidio) being installed.
        from screencap.engine.db import get_session_for_path  # noqa: PLC0415
        from screencap.engine.db.models import NetworkEventMeta  # noqa: PLC0415
        from screencap.network import crypto  # noqa: PLC0415
        from screencap.privacy import (  # noqa: PLC0415
            Anonymizer,
            create_default_pipeline,
        )

        self._recording_id = recording_id

        # Fetch meta row in a short-lived session.
        session = get_session_for_path(db_path)
        try:
            meta = (
                session.query(NetworkEventMeta)
                .filter(NetworkEventMeta.recording_id == recording_id)
                .one_or_none()
            )
        finally:
            session.close()
        if meta is None:
            raise KekUnavailableError(
                f"recording {recording_id} has no network_event_meta row "
                "(V1-vintage recording or non-network capture)"
            )

        # KEK lookup is READ-ONLY at export time. ``get_kek()`` returns
        # None when the Keychain entry is absent (e.g. the user ran
        # ``screencap network remove-kek``); we surface that as
        # KekUnavailableError rather than silently regenerating a fresh
        # KEK -- which would then fail the unwrap with a confusing
        # InvalidTag, AND make ``network remove-kek`` non-sticky.
        try:
            kek = crypto.get_kek()
        except Exception as exc:
            raise KekUnavailableError(f"KEK unavailable: {exc}") from exc
        if kek is None:
            raise KekUnavailableError(
                f"KEK is not present in the Keychain "
                f"(service={crypto.SERVICE!r}, account={crypto.KEK_ACCOUNT!r}). "
                "If you removed it via `screencap network remove-kek`, "
                "any prior encrypted recordings are now undecryptable. "
                "Run `screencap network uninstall && screencap start "
                "--network` to regenerate a fresh KEK; existing encrypted "
                "bodies will be lost."
            )

        # Unwrap the per-recording DEK once. ``InvalidTag`` here means
        # either the wrapped DEK is corrupt or the KEK has rotated and
        # the wrapped DEK no longer matches -- fail-loud at construction
        # so the caller can surface an actionable error.
        try:
            self._dek: bytes = crypto.unwrap_dek(
                bytes(meta.dek_wrapped),
                bytes(meta.dek_nonce),
                kek,
            )
        except Exception as exc:
            raise KekUnavailableError(
                f"DEK unwrap failed for recording {recording_id}: {exc}"
            ) from exc

        # Drop KEK plaintext now -- DEK is what we hold for the rest of
        # the pipeline's lifetime. Python doesn't guarantee zeroing but
        # this at least drops the binding; process death is the real
        # cleanup boundary per crypto.py's documented memory hygiene.
        del kek

        # Detection pipeline + anonymizer for plaintext bodies. Built
        # once per pipeline -- detector constructors load NLP models
        # (~200ms-2s) which we never want to pay per row.
        self._detector = create_default_pipeline()
        self._anonymizer = Anonymizer()

    def decrypt_and_scrub(self, capture_event: Any) -> Any:
        """Convert a capture-side network event to its export-side form.

        Looks up the matching export-side class for ``capture_event``,
        attempts AES-GCM decrypt of ``body_ciphertext`` (if present),
        runs the detection pipeline over the plaintext, and returns an
        export-side event with the scrubbed plaintext in ``body_text``.

        The export-side class never carries ``body_ciphertext`` /
        ``body_nonce`` / ``body_aad`` -- ciphertext can never escape via
        this transform by construction.

        Failure modes (each yields an export-side event with
        ``body_text=None`` plus a debug-log entry, NOT a raise):

        * Capture event has ``body_ciphertext is None``: V1-vintage row
          or metadata-only path -- export event has ``body_text=None``
          unconditionally.
        * Capture event has ciphertext but ``body_aad is None``:
          integrity error (the AAD-NOT-NULL invariant is violated for a
          ciphertext-bearing row). Skip decrypt.
        * Capture event's stored ``body_aad`` differs from the AAD
          recomputed from its public fields: AAD format drift between
          capture and export. Skip decrypt.
        * AES-GCM decrypt raises ``InvalidTag``: tampered ciphertext or
          DEK mismatch. Skip decrypt.

        Args:
            capture_event: A :class:`NetworkRequestEvent`,
                :class:`NetworkResponseEvent`,
                :class:`NetworkWebSocketUpgradeEvent`, or
                :class:`NetworkWebSocketFrameEvent`. The event MUST
                belong to the ``recording_id`` this pipeline was
                constructed for (the caller is responsible for filtering
                -- no cross-recording leakage by construction).

        Returns:
            The matching export-side event class with ``body_text``
            populated (or ``None`` when no ciphertext was present or
            the per-row decrypt failed).

        Raises:
            ValueError: ``capture_event`` is not one of the four
                supported network event classes. Programming error;
                callers should never pass a non-network event here.
        """
        # Lazy import to keep module importable without heavy deps.
        from screencap.engine.events import (  # noqa: PLC0415
            EventType,
            NetworkRequestEvent,
            NetworkRequestExportEvent,
            NetworkResponseEvent,
            NetworkResponseExportEvent,
            NetworkWebSocketFrameEvent,
            NetworkWebSocketFrameExportEvent,
            NetworkWebSocketUpgradeEvent,
            NetworkWebSocketUpgradeExportEvent,
        )
        from screencap.network import crypto  # noqa: PLC0415

        # Map capture-side -> (export class, event_type discriminator).
        if isinstance(capture_event, NetworkRequestEvent):
            export_cls = NetworkRequestExportEvent
            event_type_value = EventType.NETWORK_REQUEST.value
        elif isinstance(capture_event, NetworkResponseEvent):
            export_cls = NetworkResponseExportEvent
            event_type_value = EventType.NETWORK_RESPONSE.value
        elif isinstance(capture_event, NetworkWebSocketUpgradeEvent):
            export_cls = NetworkWebSocketUpgradeExportEvent
            event_type_value = EventType.NETWORK_WS_UPGRADE.value
        elif isinstance(capture_event, NetworkWebSocketFrameEvent):
            export_cls = NetworkWebSocketFrameExportEvent
            event_type_value = EventType.NETWORK_WS_FRAME.value
        else:
            raise ValueError(
                "decrypt_and_scrub: unsupported capture event type: "
                f"{type(capture_event).__name__}"
            )

        body_text: str | None = None
        ciphertext = getattr(capture_event, "body_ciphertext", None)
        nonce = getattr(capture_event, "body_nonce", None)
        aad = getattr(capture_event, "body_aad", None)

        if ciphertext is not None:
            # Pre-decrypt integrity check: distinguishes missing-AAD
            # from genuine ciphertext tampering. Both manifest as
            # ``InvalidTag`` post-decrypt; this check separates them so
            # the audit log can tell the two apart.
            if aad is None:
                logger.debug(
                    "decrypt_and_scrub: body_aad is None for "
                    "ciphertext-bearing event flow_id=%s -- integrity "
                    "error, skipping decrypt",
                    getattr(capture_event, "flow_id", "?"),
                )
            elif nonce is None:
                # ``body_nonce`` MUST be present whenever ciphertext is
                # present (the schema co-populates them in the addon).
                # Treat missing-nonce as the same class of integrity
                # error as missing-AAD.
                logger.debug(
                    "decrypt_and_scrub: body_nonce is None for "
                    "ciphertext-bearing event flow_id=%s -- integrity "
                    "error, skipping decrypt",
                    getattr(capture_event, "flow_id", "?"),
                )
            else:
                # Reconstruct the AAD from event fields and compare
                # against the stored bytes. Detects rows whose stored
                # AAD bytes drifted from what aad_bytes() would produce
                # today (e.g., a future AAD-formula version).
                expected_aad = crypto.aad_bytes(
                    recording_id=self._recording_id,
                    flow_id=capture_event.flow_id,
                    event_type=event_type_value,
                    ts_ns=capture_event.timestamp_ns,
                )
                if expected_aad != aad:
                    logger.debug(
                        "decrypt_and_scrub: AAD mismatch for flow_id=%s "
                        "(stored=%r recomputed=%r)",
                        capture_event.flow_id,
                        aad,
                        expected_aad,
                    )
                else:
                    try:
                        plaintext = crypto.decrypt_body(
                            ciphertext, nonce, self._dek, aad,
                        )
                    except Exception as exc:
                        logger.debug(
                            "decrypt_and_scrub: decryption failed for "
                            "flow_id=%s: %s",
                            capture_event.flow_id,
                            exc,
                        )
                    else:
                        # Decode + scrub. Bodies are usually JSON / text
                        # but binary payloads are possible (WS binary
                        # frames). Use utf-8 with replace so the
                        # detector input is meaningful even on binary.
                        try:
                            decoded = plaintext.decode("utf-8")
                        except UnicodeDecodeError:
                            decoded = plaintext.decode(
                                "utf-8", errors="replace",
                            )
                        body_text = self._scrub_text(decoded)

        # Build the export event by copying public fields then
        # overriding body fields. ``model_dump`` preserves the public
        # field shape; we drop the ciphertext columns and add
        # ``body_text``. If ``timestamp`` is missing on the capture
        # event (defensive -- BaseEvent always carries it), the export
        # class's required ``timestamp`` Field would surface that as a
        # validation error -- the caller-side test ensures the contract.
        data = capture_event.model_dump()
        for key in ("body_ciphertext", "body_nonce", "body_aad"):
            data.pop(key, None)
        data["body_text"] = body_text
        return export_cls(**data)

    def _scrub_text(self, text: str) -> str:
        """Run the detection pipeline over ``text`` and apply Anonymizer.

        Returns the anonymized form -- detected entity spans are
        replaced with ``<ENTITY_TYPE>`` tags. The detection pipeline
        normalizes the text first (NFKC + strip zero-width); the
        anonymizer is run against the normalized text since detection
        offsets refer to that form.
        """
        result = self._detector.detect(text)
        return self._anonymizer.anonymize(
            result.normalized_text, result.detections,
        )


def recording_has_encrypted_bodies(db_path: str, recording_id: int) -> bool:
    """Return ``True`` iff the recording has a ``network_event_meta`` row.

    Cheap pre-check used by callers that want to decide between the V1
    metadata-only path (no scrub pipeline) and V1.5 (with pipeline)
    without paying the Keychain prompt cost on V1-vintage recordings.

    Args:
        db_path: Path to the recording's ``recording.db`` file.
        recording_id: ID of the recording to check.

    Returns:
        ``True`` when a meta row exists for this recording (V1.5
        recording with at least one encrypted body candidate); ``False``
        otherwise (V1-vintage, non-network, or V1.5 recording where the
        proxy crashed before the meta row was inserted).
    """
    from screencap.engine.db import get_session_for_path  # noqa: PLC0415
    from screencap.engine.db.models import NetworkEventMeta  # noqa: PLC0415

    session = get_session_for_path(db_path)
    try:
        meta = (
            session.query(NetworkEventMeta)
            .filter(NetworkEventMeta.recording_id == recording_id)
            .one_or_none()
        )
    finally:
        session.close()
    return meta is not None


__all__ = [
    "KekUnavailableError",
    "NetworkScrubPipeline",
    "recording_has_encrypted_bodies",
]
