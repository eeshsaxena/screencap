"""Export recording events as JSONL."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from screencap import __version__

logger = logging.getLogger(__name__)


def build_export_metadata(exclude_moves: bool) -> dict:
    """Build metadata dict for the JSONL header line."""
    return {
        "_meta": True,
        "format_version": 2,
        "screencap_version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exclude_moves": exclude_moves,
    }


def export_recording(
    recording_dir,
    output_path: str | None,
    exclude_moves: bool,
    metadata: dict | None = None,
) -> int:
    """Export a single recording to JSONL.

    Returns event count on success, or -1 if the recording uses the
    legacy capture.db format (unsupported).

    When *output_path* is a file path, uses atomic write (write to .tmp,
    rename on success).  When *output_path* is None, writes to stdout.
    """
    from sc_engine import Capture

    try:
        with Capture.load(str(recording_dir)) as capture:
            if output_path is None:
                # Write to stdout — no atomic write needed
                return _write_events(capture, sys.stdout, exclude_moves, metadata)

            # Atomic write: .tmp → rename
            tmp_path = output_path + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    count = _write_events(capture, f, exclude_moves, metadata)
                os.rename(tmp_path, output_path)
                return count
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
    except FileNotFoundError:
        return -1


def _write_events(
    capture,
    out_file,
    exclude_moves: bool,
    metadata: dict | None,
    privacy_filter=None,
) -> int:
    """Stream events to an open file handle. Returns event count.

    Args:
        capture: CaptureSession instance.
        out_file: Writable file object.
        exclude_moves: Whether to exclude mouse.move events.
        metadata: Optional metadata dict for header line.
        privacy_filter: Optional callable(WindowSwitchEvent) -> WindowSwitchEvent | None.
            Returns None to suppress the event, or a modified event (e.g. masked title).
    """
    import click

    from sc_engine.events import WindowSwitchEvent

    if metadata is not None:
        click.echo(json.dumps(metadata), file=out_file)

    count = 0
    for event in capture.export_events(include_moves=not exclude_moves):
        if isinstance(event, WindowSwitchEvent) and privacy_filter is not None:
            event = privacy_filter(event)
            if event is None:
                continue
        click.echo(event.model_dump_json(), file=out_file)
        count += 1
    return count


def build_privacy_filter(
    privacy_mode: str = "internal",
    cloud_intent: bool = False,
):
    """Build a privacy filter callback for window.switch events.

    Args:
        privacy_mode: Privacy mode string (public/shared/internal).
        cloud_intent: Whether this export is destined for cloud upload.

    Returns:
        Callable that takes a WindowSwitchEvent and returns the event
        (possibly with masked title), or None to suppress it.
    """
    from screencap.privacy.actions import PrivacyAction
    from screencap.privacy.context import DefaultContextClassifier
    from screencap.privacy.policy import (
        DefaultPolicyEvaluator,
        PrivacyMode,
    )

    try:
        mode = PrivacyMode(privacy_mode)
    except ValueError:
        mode = PrivacyMode.INTERNAL

    # Load privacy config from config.toml
    try:
        from screencap.config import get_privacy_config

        privacy_cfg = get_privacy_config()
    except (ImportError, FileNotFoundError, KeyError, ValueError):
        logger.debug("Could not load privacy config, using defaults")
        from screencap.privacy.policy import PrivacyConfig

        privacy_cfg = PrivacyConfig(mode=mode)

    classifier = DefaultContextClassifier(app_classes=privacy_cfg.app_classes)
    evaluator = DefaultPolicyEvaluator(privacy_cfg)

    def _filter(event):
        from screencap.privacy.policy import FrameMetadata

        bundle_id = event.app_bundle_id or ""
        metadata = FrameMetadata(
            bundle_id=bundle_id,
            window_title=event.window_title,
            timestamp=event.timestamp,
        )
        ctx = classifier.classify(metadata)
        decision = evaluator.evaluate(ctx, metadata, mode)
        action = decision.action

        if action == PrivacyAction.EXCLUDE:
            return None

        if cloud_intent and action == PrivacyAction.OCR_FALLBACK:
            return None

        if action == PrivacyAction.MASK_WINDOW:
            return event.model_copy(update={
                "window_title": event.app_name,
            })

        return event

    return _filter
