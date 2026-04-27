"""Privacy filter constructors for window.switch events.

Neutral home (no CLI imports) for the cloud-bound window-event filter so
both ``CaptureSession`` exports, the chunk processor, and recovery can
import from the same module.

Two public constructors:

- ``build_privacy_filter`` — generic filter constructor used by the CLI
  ``screencap export`` path and called internally by the cloud factory.
- ``build_cloud_window_filter`` — sanctioned constructor for cloud-bound
  callers. Returns ``None`` when ``cloud_bound=False`` and a cloud-mode
  filter when ``cloud_bound=True``. The ``cloud_bound`` flag dispatches
  *unconditionally* at the call site so a future caller cannot
  accidentally skip the filter via a forgotten ``if/else``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from screencap.engine.events import WindowSwitchEvent

logger = logging.getLogger(__name__)


def build_privacy_filter(
    privacy_mode: str = "internal",
    cloud_intent: bool = False,
    capture_dir: Path | None = None,
) -> Callable[[WindowSwitchEvent], WindowSwitchEvent | None]:
    """Build a privacy filter callback for window.switch events.

    Args:
        privacy_mode: Privacy mode string (public/shared/internal).
        cloud_intent: Whether this export is destined for cloud upload.
        capture_dir: Path to the capture directory (for loading menu bar
            overrides from ``.menubar_overrides.json``).

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

    # Cloud uploads must use the strictest non-shared mode to prevent
    # leaking window titles for apps like Slack/Teams (see action matrix).
    if cloud_intent:
        mode = PrivacyMode.PUBLIC

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

    # Load session overrides from menu bar toggles
    runtime_overrides: dict[str, str] = {}
    if capture_dir is not None:
        override_path = Path(capture_dir) / ".menubar_overrides.json"
        if override_path.exists():
            try:
                runtime_overrides = json.loads(override_path.read_text())
            except Exception:
                logger.debug("Could not load menu bar overrides")

    def _filter(event: WindowSwitchEvent) -> WindowSwitchEvent | None:
        from screencap.privacy.policy import FrameMetadata

        # Fail-closed on null/empty/whitespace bundle_id (R17). macOS
        # accessibility sometimes fires a window event before bundle_id
        # is resolved; without a bundle_id we cannot classify the app,
        # so suppress the event rather than leak the original title.
        if not (event.app_bundle_id or "").strip():
            return None

        bundle_id = event.app_bundle_id

        # Check runtime overrides first (user toggles from menu bar)
        if runtime_overrides:
            from screencap.privacy.actions import resolve_override
            from screencap.privacy.domain_loader import extract_root_domain

            domain = getattr(event, "domain", None)
            root_domain = extract_root_domain(domain) if domain else None
            override_action = resolve_override(
                runtime_overrides, bundle_id, root_domain,
            )
            if override_action == "exclude":
                return None
            if override_action == "allow":
                return event

        metadata = FrameMetadata(
            bundle_id=bundle_id,
            window_title=event.window_title,
            domain=event.domain,
            timestamp=event.timestamp,
        )
        ctx = classifier.classify(metadata)
        decision = evaluator.evaluate(ctx, metadata, mode)
        action = decision.action

        if action == PrivacyAction.EXCLUDE:
            return None

        if action == PrivacyAction.MASK_WINDOW:
            return event.model_copy(update={
                "window_title": event.app_name,
                "domain": None,
            })

        return event

    return _filter


def build_cloud_window_filter(
    cloud_bound: bool,
    privacy_mode: str = "internal",
    capture_dir: Path | None = None,
) -> Callable[[WindowSwitchEvent], WindowSwitchEvent | None] | None:
    """Sanctioned constructor for cloud-bound window-event filtering.

    Cloud-bound callers MUST go through this factory rather than calling
    ``build_privacy_filter`` directly. The ``cloud_bound`` argument is the
    structural switch: ``False`` returns ``None`` (no filter applied);
    ``True`` returns a filter built with ``cloud_intent=True`` (forces
    PUBLIC mode regardless of the configured ``privacy_mode``).

    Returning ``None`` for the non-cloud path lets call sites wire the
    factory unconditionally (``window_filter=build_cloud_window_filter(
    self._cloud_intent, ...)``) — no caller-side ``if/else`` to forget.

    Args:
        cloud_bound: ``True`` if the events are destined for cloud upload.
        privacy_mode: Configured privacy mode string. Ignored when
            ``cloud_bound=True`` (cloud forces PUBLIC).
        capture_dir: Path to the capture directory for ``.menubar_overrides.json``.

    Returns:
        A filter callable when ``cloud_bound=True``, or ``None`` when
        ``cloud_bound=False``.
    """
    if not cloud_bound:
        return None
    return build_privacy_filter(
        privacy_mode=privacy_mode,
        cloud_intent=True,
        capture_dir=capture_dir,
    )
