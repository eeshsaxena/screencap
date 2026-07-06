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
- Multiple blocking sources: secure field, excluded app, and policy
  block are tracked independently with per-source hold timers.
  Secure input (CGSIsSecureEventInputSet) is tracked separately and
  only blocks keystrokes — not screenshots or video — because the
  flag is system-wide and a background app enabling it should not
  block screen capture of an allowed foreground app.
- Transition hold: 1.0s after a blocking source deactivates before
  resuming capture. Covers macOS app-switch animations (200-350ms)
  with margin. The cost is a few missed frames of the new (allowed) app,
  which is acceptable since post-processing preserves them regardless.
- Thread-safe: called from the event_processor thread in screencap.engine.
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

import json
import logging
import queue as _queue_mod
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from screencap.privacy.actions import (
    BLOCK_ACTIONS,
    KEYSTROKE_CONTENT_FIELDS,
    KEYSTROKE_NULL_ACTIONS,
    VIDEO_BLOCK_ACTIONS,
    PrivacyAction,
    resolve_override,
)
from screencap.privacy.classify import DefaultContextClassifier, domain_from_url
from screencap.privacy.domain_loader import extract_root_domain
from screencap.privacy.policy import (
    DEFAULT_TRANSITION_HOLD_SECONDS,
    DefaultPolicyEvaluator,
    FrameMetadata,
    PrivacyConfig,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureDisposition:
    """Per-event capture decision with separate screen/video/keystroke gates.

    Allows MASK_WINDOW apps to pass screenshots through while still
    blocking video frames and nulling keystrokes.
    """

    screen_allowed: bool  # True for MASK_WINDOW, False for EXCLUDE
    video_allowed: bool  # False for both MASK_WINDOW and EXCLUDE
    keystrokes_allowed: bool  # False for both MASK_WINDOW and EXCLUDE


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
      password manager, etc.)  [blocks screen + video + keystrokes]
    - ``secure_input`` — from macOS CGSIsSecureEventInputSet
      [blocks keystrokes ONLY — the flag is system-wide so a
      background app enabling it must not block an allowed foreground
      app's screenshots/video]
    - ``secure_field`` — from AXSecureTextField in element_state
      [blocks screen + video + keystrokes]

    ``is_screen_allowed()`` returns False if ``app_policy``,
    ``secure_field``, ``filter_error``, or ``initial`` is active.
    ``secure_input`` only gates keystrokes.

    Usage from screencap.engine integration::

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
        window_feed_q=None,
        override_q=None,
        override_file=None,
        block_video: bool = True,
    ) -> None:
        self._evaluator = DefaultPolicyEvaluator(config)
        self._classifier = DefaultContextClassifier(
            app_classes=config.app_classes,
        )
        self._lock = threading.Lock()
        self._hold_seconds = transition_hold_seconds

        self._cloud_intent = cloud_intent
        self._block_actions = BLOCK_ACTIONS
        # U4b: when ``block_video`` is False, capture-time VIDEO blocking is
        # disabled and ``video_allowed`` is always True (rich video for all
        # destinations). Screenshot gating (``screen_allowed``), keystroke
        # nulling (``keystrokes_allowed``), blocked-interval metadata, and
        # background-window screenshot masking (``mask_frame``) are
        # UNAFFECTED — those are separate mechanisms U6's post-hoc video
        # masker does not replace. The default (True) preserves today's
        # capture-time video blocking byte-for-byte. This flag is driven by
        # ``config.get_masked_video_upload_enabled()`` at the single gating
        # site in ``collaborators.build_recorder_privacy_filter``; it is
        # NOT safe to flip ON until U6+U7's live-upload cutover lands (see
        # that gating site for the full rationale).
        self._block_video = block_video

        # Keystroke/video block sets — wider than _block_actions since
        # MASK_WINDOW no longer blocks screenshots but still blocks
        # keystrokes and video frames.
        self._keystroke_null_actions = KEYSTROKE_NULL_ACTIONS
        self._video_block_actions = VIDEO_BLOCK_ACTIONS

        # Mutable state (protected by _lock)
        # reason -> hold_until monotonic timestamp (0.0 = not active)
        self._blocked_reasons: dict[str, float] = {"initial": float("inf")}
        self._current_bundle_id: str = ""
        self._current_title: str = ""
        # Cached most-recent window event so poll_overrides can replay
        # the gating evaluation immediately when a new override arrives
        # — without this, the override only takes effect on the NEXT
        # real window event, which may be many seconds away if the user
        # stays on the same tab.
        self._current_window_data: dict | None = None

        # Blocked interval tracking (for cloud-intent manifest metadata)
        self._blocked_intervals: list[dict] = []
        self._current_block_start: float | None = None

        # Menu bar IPC
        self._window_feed_q = window_feed_q
        self._override_q = override_q
        self._override_file = override_file
        self._runtime_overrides: dict[str, str] = {}

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

    @property
    def cloud_intent(self) -> bool:
        return self._cloud_intent

    def on_window_event(self, window_data: dict) -> None:
        """Update blocked state from a window event.

        Called from the event_processor thread when a window event
        arrives. Must be fast (<1ms) to stay off the hot path.

        Args:
            window_data: The window event data dict from screencap.engine,
                containing at least 'app_bundle_id' and 'title'.
        """
        bundle_id = window_data.get("app_bundle_id") or ""
        title = window_data.get("title") or ""

        # Extract domain from browser_url for domain-aware classification
        domain = None
        browser_url = window_data.get("browser_url")
        if browser_url:
            domain = domain_from_url(browser_url)
            logger.debug("Domain from browser_url: %s", domain)

        # Check runtime overrides (user toggles from menu bar)
        override_action_str = None
        if self._runtime_overrides:
            override_domain = extract_root_domain(domain) if domain else None
            with self._lock:
                override_action_str = resolve_override(
                    self._runtime_overrides, bundle_id, override_domain,
                )

        if override_action_str is not None:
            effective_action = PrivacyAction(override_action_str)
        else:
            meta = FrameMetadata(
                bundle_id=bundle_id,
                window_title=title,
                domain=domain,
                timestamp=time.monotonic(),
                browser_url=browser_url,
            )
            ctx = self._classifier.classify(meta)
            decision = self._evaluator.evaluate(ctx, meta)
            effective_action = decision.action
        now_blocked = effective_action in self._block_actions
        # Whether keystrokes/video need blocking (wider than screenshot blocking)
        now_keystroke_blocked = effective_action in self._keystroke_null_actions
        now_video_blocked = effective_action in self._video_block_actions

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

            # Track keystroke/video blocking independently of screenshot blocking.
            # MASK_WINDOW apps don't set app_policy (screenshots pass) but
            # still need keystroke nulling and video dropping.
            was_ks_blocked = "app_keystrokes" in self._blocked_reasons
            if now_keystroke_blocked:
                self._blocked_reasons["app_keystrokes"] = float("inf")
            elif was_ks_blocked:
                self._blocked_reasons["app_keystrokes"] = now + self._hold_seconds

            was_vid_blocked = "app_video" in self._blocked_reasons
            if now_video_blocked:
                self._blocked_reasons["app_video"] = float("inf")
            elif was_vid_blocked:
                self._blocked_reasons["app_video"] = now + self._hold_seconds

            self._current_bundle_id = bundle_id
            self._current_title = title
            self._current_window_data = window_data

        # Feed window event summary to menu bar (non-blocking, best-effort)
        if self._window_feed_q is not None:
            try:
                display_domain = extract_root_domain(domain) if domain else None
                self._window_feed_q.put_nowait({
                    "app_name": window_data.get("app_name") or "",
                    "bundle_id": bundle_id,
                    "domain": display_domain,
                    "action": effective_action.value,
                    "ts": time.time(),
                })
            except Exception:
                pass  # Menu bar feed is non-critical

    def poll_overrides(self) -> None:
        """Drain the override queue from the menu bar (non-blocking).

        Called from ``process_events()`` on every event iteration so a
        user toggle takes effect within one event cycle even when no
        new window event arrives. When a new override is drained, the
        most-recently-cached window state is re-evaluated against the
        new override so the filter's blocked state updates immediately.
        """
        if self._override_q is None:
            return
        changed = False
        while True:
            try:
                override = self._override_q.get_nowait()
            except (_queue_mod.Empty, OSError):
                break
            key = override["key"]
            action = override["action"]
            with self._lock:
                self._runtime_overrides[key] = action
            changed = True
        if not changed:
            return
        # Batch-write once after draining all pending overrides
        if self._override_file is not None:
            try:
                self._override_file.write_text(
                    json.dumps(self._runtime_overrides, indent=2)
                )
            except Exception:
                pass
        # Replay the gating evaluation against the cached current window
        # so the new override takes effect immediately. Without this, the
        # filter's _blocked_reasons["app_policy"] stays in whatever state
        # the LAST real window event left it, and the user keeps capturing
        # the disabled target until they switch tabs.
        with self._lock:
            cached = self._current_window_data
        if cached is not None:
            try:
                self.on_window_event(cached)
            except Exception:
                # Re-evaluation must never crash the recorder; fail safe
                # by setting the filter-error block reason.
                self.fail_closed()

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

    def _expire_holds(self, now: float) -> None:
        """Remove expired hold timers (must be called with _lock held)."""
        expired = []
        for reason, hold_until in self._blocked_reasons.items():
            if hold_until != float("inf") and now > hold_until:
                expired.append(reason)
        for reason in expired:
            del self._blocked_reasons[reason]

    def is_screen_allowed(self, timestamp: float | None = None) -> bool:
        """Check whether screen capture is currently allowed.

        Backward-compatible wrapper around ``get_capture_disposition()``.

        Args:
            timestamp: Ignored (kept for API compatibility).

        Returns:
            True if screenshot capture should proceed, False if blocked.
        """
        return self.get_capture_disposition().screen_allowed

    def get_capture_disposition(self, timestamp: float | None = None) -> CaptureDisposition:
        """Return separate screen/video/keystroke capture decisions.

        Also checks macOS Secure Input mode (Layer 0) on each call.

        - ``screen_allowed``: True unless an EXCLUDE app or
          secure_field/fail-closed is active.
        - ``video_allowed``: False for EXCLUDE and MASK_WINDOW apps,
          plus secure_field/fail-closed.
        - ``keystrokes_allowed``: False for EXCLUDE and MASK_WINDOW apps,
          plus secure_input/secure_field/fail-closed.

        Args:
            timestamp: Ignored (kept for API compatibility).
        """
        now = time.monotonic()
        with self._lock:
            self._check_secure_input()
            self._expire_holds(now)

            # Screen blocking: EXCLUDE + secure_field + errors.
            # secure_input is intentionally excluded here — it is a
            # system-wide macOS flag that background apps can trigger.
            # A background password manager enabling Secure Input must
            # not block an allowed foreground app's screenshots.
            screen_block_reasons = {
                "app_policy", "secure_field",
                "filter_error", "initial",
            }
            screen_blocked = bool(
                self._blocked_reasons.keys() & screen_block_reasons
            )

            # Keystroke blocking: adds secure_input (keystroke-specific
            # protection — what CGSIsSecureEventInputSet is actually
            # about) and app_keystrokes (MASK_WINDOW apps).
            keystroke_block_reasons = screen_block_reasons | {
                "app_keystrokes", "secure_input",
            }
            keystrokes_blocked = bool(
                self._blocked_reasons.keys() & keystroke_block_reasons
            )

            # U4b: when capture-time video blocking is disabled (flag ON),
            # video is always allowed (rich capture). Screenshot/keystroke
            # gating above is unchanged.
            if self._block_video:
                video_block_reasons = screen_block_reasons | {"app_video"}
                video_blocked = bool(
                    self._blocked_reasons.keys() & video_block_reasons
                )
            else:
                video_blocked = False

            return CaptureDisposition(
                screen_allowed=not screen_blocked,
                video_allowed=not video_blocked,
                keystrokes_allowed=not keystrokes_blocked,
            )

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

    def mask_frame(self, image, geometry: dict | None, pixel_ratio: float) -> None:
        """Mask sensitive background window regions in a screenshot, in-place.

        Evaluates every visible window against the privacy policy (forced to
        public mode) and applies solid masks over windows whose action is
        EXCLUDE or MASK_WINDOW.

        This catches sensitive apps (Slack, email, terminals) visible in the
        background that the foreground-only capture-time filter cannot block.
        Runs for both local and cloud-intent recordings — ``mask_frame()``
        only modifies pixel data in-place and never deletes local files.

        Args:
            image: PIL Image to mask in-place.
            geometry: Window geometry dict with "windows" and "display_bounds".
            pixel_ratio: Retina scaling factor (e.g. 2.0).
        """
        if geometry is None:
            return

        windows = geometry.get("windows")
        if not windows:
            return

        try:
            from screencap.privacy.mask_primitives import (
                _apply_bitmap_mask_to_image,
                _apply_mask_to_image,
                window_regions_from_geometry,
            )
            from screencap.privacy.policy import PrivacyMode

            # Force public mode for cloud masking so CHAT/EMAIL/etc. get
            # MASK_WINDOW instead of TEXT_REDACT (which can't mask pixels).
            # The allow set is restricted to confirmed entries (SCR-235
            # KTD5) so this PUBLIC-forcing surface agrees with the cloud
            # window filter: confirmed apps stay unmasked, legacy entries
            # take the matrix action exactly as before.
            if not hasattr(self, "_masking_evaluator"):
                from dataclasses import replace as _dc_replace

                from screencap.privacy.policy import DefaultPolicyEvaluator
                _cloud_config = _dc_replace(
                    self._evaluator.config.restricted_to_confirmed(),
                    mode=PrivacyMode.PUBLIC,
                )
                self._masking_evaluator = DefaultPolicyEvaluator(_cloud_config)

            display_bounds = geometry.get("display_bounds", (0, 0, 0, 0))
            display_origin = (display_bounds[0], display_bounds[1])

            regions = window_regions_from_geometry(
                windows,
                image.width,
                image.height,
                pixel_ratio,
                self._classifier,
                self._masking_evaluator,
                display_origin=display_origin,
                respect_z_order=True,
            )
            if regions:
                mask_bmp = getattr(regions[0], "_mask_bitmap", None)
                if mask_bmp is not None:
                    try:
                        _apply_bitmap_mask_to_image(image, mask_bmp)
                    finally:
                        mask_bmp.close()
                else:
                    _apply_mask_to_image(image, regions)
        except Exception:
            pass  # Never block the recording pipeline on masking errors

    @staticmethod
    def null_keystroke_content(action_data: dict) -> None:
        """Null out keystroke content fields in an action event dict.

        Preserves structural metadata (timestamp, action name, event type).
        Mouse and window events pass through unchanged.
        """
        for field in KEYSTROKE_CONTENT_FIELDS:
            if field in action_data:
                action_data[field] = None

