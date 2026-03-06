"""Capture-time privacy enforcement for the recording pipeline.

Observes window events during recording and evaluates them against the
privacy policy to determine whether screen capture should proceed.

This is the strongest privacy enforcement point: blocked-app frames are
never stored to disk. Post-processing (scrubber.py) remains as
defense-in-depth for anything this layer cannot catch (e.g., browser
tab-level content, OCR-based redaction).

Design decisions:
- Screenshots only: action events (keystrokes, mouse) and window events
  are still captured during blocked periods. Post-processing handles
  those. See docs/tickets/ for the follow-up to extend this.
- Transition hold: 1.0s after switching FROM a blocked app, capture
  remains suppressed. This covers macOS app-switch animations (200-350ms)
  with margin. The cost is a few missed frames of the new (allowed) app,
  which is acceptable since post-processing preserves them regardless.
- Thread-safe: called from the event_processor thread in sc_engine.
- No ScreenCaptureKit native boundary yet — this is Python-layer
  enforcement that filters in process_events() before screen events
  reach the write queue. A native helper is the future path for true
  pre-capture exclusion (see DESIGN section below).

DESIGN: ScreenCaptureKit native boundary (future)
--------------------------------------------------
The ideal capture-time enforcement uses SCContentFilter to exclude
app windows at the ScreenCaptureKit level, so blocked-app pixels are
never delivered to Python at all. This requires:

1. A native helper (Swift/ObjC) that owns the SCStream and applies
   SCContentFilter with excluding-applications / excluding-windows.
2. A narrow IPC contract: screencap sends (blocked_bundle_ids, mode)
   to the helper; the helper manages the SCStream filter updates.
3. Dynamic filter updates during recording when policy or frontmost
   app changes.

This is deferred because:
- The current capture uses mss/screencapture CLI, not ScreenCaptureKit
- The native helper is a significant complexity/risk increase
- Python-layer filtering achieves the same privacy outcome for stored
  data, just with slightly higher CPU (screenshots are taken then
  discarded rather than never taken)
"""

from __future__ import annotations

import threading
import time

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.context import DefaultContextClassifier
from screencap.privacy.policy import (
    DefaultPolicyEvaluator,
    FrameMetadata,
    PrivacyConfig,
)

# Default transition hold: suppress capture for this many seconds after
# switching away from a blocked app. Covers macOS app-switch animations
# (Cmd+Tab ~200-350ms) with margin.
DEFAULT_TRANSITION_HOLD_SECONDS: float = 1.0

# Actions that mean "this app should not be captured"
_BLOCK_ACTIONS = frozenset({PrivacyAction.EXCLUDE, PrivacyAction.MASK_WINDOW})


class RecorderPrivacyFilter:
    """Capture-time privacy filter for the recording pipeline.

    Observes window events in real-time and evaluates them against the
    privacy policy to gate screen capture. Thread-safe.

    Usage from sc_engine integration::

        filter = RecorderPrivacyFilter(privacy_config)

        # In process_events(), on window event:
        filter.on_window_event(event.data)

        # Before saving a screen event:
        if not filter.is_screen_allowed(timestamp):
            skip this screenshot
    """

    def __init__(
        self,
        config: PrivacyConfig,
        transition_hold_seconds: float = DEFAULT_TRANSITION_HOLD_SECONDS,
    ) -> None:
        self._evaluator = DefaultPolicyEvaluator(config)
        self._classifier = DefaultContextClassifier()
        self._lock = threading.Lock()
        self._hold_seconds = transition_hold_seconds

        # Mutable state (protected by _lock)
        self._blocked = False
        self._transition_hold_until: float = 0.0
        self._current_bundle_id: str = ""
        self._current_title: str = ""
        self._block_reason: str = ""

    def on_window_event(self, window_data: dict) -> None:
        """Update blocked state from a window event.

        Called from the event_processor thread when a window event
        arrives. Must be fast (<1ms) to stay off the hot path.

        Args:
            window_data: The window event data dict from sc_engine,
                containing at least 'app_bundle_id' and 'title'.
        """
        bundle_id = window_data.get("app_bundle_id") or ""
        title = window_data.get("title") or ""

        meta = FrameMetadata(
            bundle_id=bundle_id,
            window_title=title,
            timestamp=time.time(),
        )
        ctx = self._classifier.classify(meta)
        decision = self._evaluator.evaluate(ctx, meta)
        now_blocked = decision.action in _BLOCK_ACTIONS

        with self._lock:
            was_blocked = self._blocked

            if was_blocked and not now_blocked:
                # Transitioning from blocked → allowed: apply hold
                self._transition_hold_until = time.time() + self._hold_seconds

            self._blocked = now_blocked
            self._current_bundle_id = bundle_id
            self._current_title = title
            self._block_reason = decision.reason if now_blocked else ""

    def is_screen_allowed(self, timestamp: float | None = None) -> bool:
        """Check whether screen capture is currently allowed.

        Args:
            timestamp: Optional Unix timestamp. If not provided, uses
                current time. Used for transition hold check.

        Returns:
            True if capture should proceed, False if blocked.
        """
        now = timestamp if timestamp is not None else time.time()
        with self._lock:
            if self._blocked:
                return False
            if now < self._transition_hold_until:
                return False
            return True

    @property
    def is_blocked(self) -> bool:
        """Whether the current frontmost app is blocked (no hold logic)."""
        with self._lock:
            return self._blocked

    @property
    def current_bundle_id(self) -> str:
        with self._lock:
            return self._current_bundle_id

    @property
    def block_reason(self) -> str:
        with self._lock:
            return self._block_reason
