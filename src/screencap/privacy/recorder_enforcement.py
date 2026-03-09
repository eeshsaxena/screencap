"""Capture-time privacy enforcement for the recording pipeline.

Observes window events during recording and evaluates them against the
privacy policy to determine whether screen capture should proceed.

This is the strongest privacy enforcement point: blocked-app frames are
never stored to disk. Post-processing (scrubber.py) remains as
defense-in-depth for anything this layer cannot catch (e.g., browser
tab-level content, OCR-based redaction).

Design decisions:
- Screenshots AND keystrokes: when any blocking reason fires, both
  screenshots are dropped and keystroke content is nulled before writing.
- Multiple blocking sources: secure input, secure field, excluded app,
  and policy block are tracked independently with per-source hold timers.
- Transition hold: 1.0s after a blocking source deactivates before
  resuming capture. Covers macOS app-switch animations (200-350ms)
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

import logging
import threading
import time
from collections.abc import Callable

from screencap.privacy.actions import KEYSTROKE_CONTENT_FIELDS, PrivacyAction
from screencap.privacy.context import DefaultContextClassifier
from screencap.privacy.policy import (
    DEFAULT_TRANSITION_HOLD_SECONDS,
    DefaultPolicyEvaluator,
    FrameMetadata,
    PrivacyConfig,
)
from screencap.privacy.reasons import ReasonCode

logger = logging.getLogger(__name__)

# Actions that mean "this app should not be captured"
# Import from actions.py — single source of truth shared with scrubber.
from screencap.privacy.actions import BLOCK_ACTIONS as _BLOCK_ACTIONS


_UNSET = object()


def _load_secure_input_fn() -> Callable[[], bool] | None:
    """Try to load CGSIsSecureEventInputSet via ctypes.

    Returns a callable that checks macOS Secure Input mode, or None if
    the symbol is unavailable (non-macOS, missing framework, etc.).
    """
    try:
        import ctypes

        cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        fn = cg.CGSIsSecureEventInputSet
        fn.restype = ctypes.c_bool
        fn.argtypes = []
        # Smoke-test the call to catch segfaults early
        fn()
        return fn
    except (OSError, AttributeError):
        return None


class RecorderPrivacyFilter:
    """Capture-time privacy filter for the recording pipeline.

    Observes window events in real-time and evaluates them against the
    privacy policy to gate screen capture and keystroke content.
    Thread-safe.

    Blocking sources are tracked independently:
    - ``app_policy`` — from window event classification (excluded app,
      password manager, etc.)
    - ``secure_input`` — from macOS CGSIsSecureEventInputSet
    - ``secure_field`` — from AXSecureTextField in element_state

    ``is_screen_allowed()`` returns False if ANY source is active or
    within its hold period.

    Usage from sc_engine integration::

        filter = RecorderPrivacyFilter(privacy_config)

        # In process_events(), on window event:
        filter.on_window_event(event.data)

        # On action event with element_state:
        filter.on_action_event(event.data)

        # Before saving a screen event:
        if not filter.is_screen_allowed(timestamp):
            skip this screenshot

        # Before writing a key event to disk:
        if not filter.is_screen_allowed():
            filter.null_keystroke_content(event.data)
    """

    def __init__(
        self,
        config: PrivacyConfig,
        transition_hold_seconds: float = DEFAULT_TRANSITION_HOLD_SECONDS,
        secure_input_fn: Callable[[], bool] | None = _UNSET,
        cloud_intent: bool = False,
    ) -> None:
        self._evaluator = DefaultPolicyEvaluator(config)
        self._classifier = DefaultContextClassifier(
            app_classes=config.app_classes,
        )
        self._lock = threading.Lock()
        self._hold_seconds = transition_hold_seconds

        # Cloud-intent: expand block set to include OCR_FALLBACK apps
        # (code editors, admin consoles) which would otherwise pass through
        # unredacted since no OCR redaction engine exists for video.
        self.cloud_intent = cloud_intent
        self._block_actions = _BLOCK_ACTIONS | (
            frozenset({PrivacyAction.OCR_FALLBACK}) if cloud_intent else frozenset()
        )

        # Mutable state (protected by _lock)
        # reason -> hold_until monotonic timestamp (0.0 = not active)
        self._blocked_reasons: dict[str, float] = {"initial": float("inf")}
        self._current_bundle_id: str = ""
        self._current_title: str = ""

        # Blocked interval tracking (for cloud-intent manifest metadata)
        self._blocked_intervals: list[dict] = []
        self._current_block_start: float | None = None

        # Secure Input detection (Layer 0)
        if secure_input_fn is _UNSET:
            # Default: try to load from OS
            self._secure_input_fn = _load_secure_input_fn()
            if self._secure_input_fn is None:
                logger.warning(
                    "CGSIsSecureEventInputSet unavailable — "
                    "Secure Input detection (Layer 0) disabled"
                )
        else:
            self._secure_input_fn = secure_input_fn

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
            timestamp=time.monotonic(),
        )
        ctx = self._classifier.classify(meta)
        decision = self._evaluator.evaluate(ctx, meta)
        now_blocked = decision.action in self._block_actions

        now = time.monotonic()
        with self._lock:
            # Clear fail-closed / initial state on successful window event
            self._blocked_reasons.pop("filter_error", None)
            self._blocked_reasons.pop("initial", None)

            was_blocked = "app_policy" in self._blocked_reasons

            if now_blocked:
                # Set app_policy hold deadline far in the future (active block)
                self._blocked_reasons["app_policy"] = float("inf")
            elif was_blocked:
                # Transitioning from blocked → allowed: start hold timer
                self._blocked_reasons["app_policy"] = now + self._hold_seconds
            # else: was not blocked, still not blocked — no change

            self._current_bundle_id = bundle_id
            self._current_title = title

    def on_action_event(self, action_data: dict) -> None:
        """Check action event for AXSecureTextField (Layer 1).

        Called from the event_processor thread when an action event
        has element_state. Checks AXRole and AXSubrole for secure
        text field indicators.

        Args:
            action_data: The action event data dict, may contain
                'element_state' with AX attributes.
        """
        element_state = action_data.get("element_state")
        if not element_state or not isinstance(element_state, dict):
            return

        is_secure = (
            element_state.get("AXRole") == "AXSecureTextField"
            or element_state.get("AXSubrole") == "AXSecureTextField"
        )

        now = time.monotonic()
        with self._lock:
            if is_secure:
                self._blocked_reasons["secure_field"] = now + self._hold_seconds
            # Don't clear secure_field here — let the hold timer expire naturally

    def _check_secure_input(self) -> None:
        """Poll macOS Secure Input mode (Layer 0).

        Called from is_screen_allowed(). Updates the secure_input
        blocking reason based on the current system state.

        Note: the ctypes call executes while self._lock is held. This is
        safe because (a) all callers run on the single event_processor
        thread (zero contention), and (b) the call is <0.1ms.
        """
        if self._secure_input_fn is None:
            return

        try:
            active = self._secure_input_fn()
        except Exception:
            return

        now = time.monotonic()
        # Lock is already held by the caller (is_screen_allowed)
        if active:
            self._blocked_reasons["secure_input"] = now + self._hold_seconds
        # Don't clear — let hold timer expire naturally

    def fail_closed(self) -> None:
        """Block all capture until the next successful window event.

        Called when on_window_event() or on_action_event() raises an
        exception, to ensure the filter doesn't fail open with stale state.
        """
        with self._lock:
            self._blocked_reasons["filter_error"] = float("inf")

    def is_screen_allowed(self, timestamp: float | None = None) -> bool:
        """Check whether screen capture is currently allowed.

        Also checks macOS Secure Input mode (Layer 0) on each call.

        Args:
            timestamp: Ignored (kept for API compatibility). The hold
                check always uses the monotonic clock internally.

        Returns:
            True if capture should proceed, False if blocked.
        """
        now = time.monotonic()
        with self._lock:
            self._check_secure_input()

            # Clean up expired hold timers and check if any are still active.
            # Strict > so a deadline of exactly `now` blocks for this cycle.
            expired = []
            for reason, hold_until in self._blocked_reasons.items():
                if hold_until != float("inf") and now > hold_until:
                    expired.append(reason)

            for reason in expired:
                del self._blocked_reasons[reason]

            return len(self._blocked_reasons) == 0

    def record_block_start(self, ts: float) -> None:
        """Record the start of a blocked interval.

        Called from process_events when the filter transitions to blocked.
        Uses event timestamps (Unix epoch) for consistency with chunk manifests.
        """
        with self._lock:
            if self._current_block_start is None:
                self._current_block_start = ts

    def record_block_end(self, ts: float, reason: str = "app_policy") -> None:
        """Record the end of a blocked interval.

        Called from process_events when the filter transitions to allowed.
        """
        with self._lock:
            if self._current_block_start is not None:
                self._blocked_intervals.append({
                    "start_ts": self._current_block_start,
                    "end_ts": ts,
                    "reason": reason,
                })
                self._current_block_start = None

    def get_blocked_intervals(self, start_ts: float, end_ts: float) -> list[dict]:
        """Get blocked intervals overlapping [start_ts, end_ts).

        Returns clipped intervals for the given chunk time range.
        Includes the current ongoing block if any.
        """
        with self._lock:
            result = []
            for interval in self._blocked_intervals:
                if interval["end_ts"] > start_ts and interval["start_ts"] < end_ts:
                    result.append({
                        "start_ts": max(interval["start_ts"], start_ts),
                        "end_ts": min(interval["end_ts"], end_ts),
                        "reason": interval["reason"],
                    })
            # Include current ongoing block
            if self._current_block_start is not None and self._current_block_start < end_ts:
                result.append({
                    "start_ts": max(self._current_block_start, start_ts),
                    "end_ts": end_ts,
                    "reason": "app_policy",
                })
            return result

    @staticmethod
    def null_keystroke_content(action_data: dict) -> None:
        """Null out keystroke content fields in an action event dict.

        Preserves structural metadata (timestamp, action name, event type).
        Mouse and window events pass through unchanged.
        """
        for field in KEYSTROKE_CONTENT_FIELDS:
            if field in action_data:
                action_data[field] = None

