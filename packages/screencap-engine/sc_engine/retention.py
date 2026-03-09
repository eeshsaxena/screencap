"""Variable-rate screenshot retention filter based on action type.

Gates screenshot saves (and implicitly action-gated video frames) using
per-action-type time floors.  Injected into ``process_events()`` between the
privacy filter and the dHash dedup gate.
"""

from __future__ import annotations

import enum


class RetentionDecision(enum.Enum):
    """Outcome of :meth:`ScreenRetentionFilter.should_save`."""

    SAVE = "save"               # Always save (click, settle)
    BYPASS_DEDUP = "bypass"     # Save, skip hash (cadence-gated)
    BASELINE = "baseline"       # Delegate to existing dHash gate
    SKIP = "skip"               # Within cadence interval, skip


_SCROLL_ACTIONS = frozenset({"scroll", "magnify", "rotate", "smart_magnify"})


class ScreenRetentionFilter:
    """Per-action-type time-floor filter for variable-rate capture."""

    __slots__ = (
        "_click_interval",
        "_drag_interval",
        "_scroll_interval",
        "_type_interval",
        "_idle_interval",
        "_settle_secs",
        "_held_buttons",
        "_drag_active",
        "_last_save_mono",
        "_settle_deadline",
        "_settle_pending",
    )

    def __init__(
        self,
        *,
        click_interval: float = 0.0,
        drag_interval: float = 0.1,
        scroll_interval: float = 0.1,
        type_interval: float = 1.0,
        idle_interval: float = 2.0,
        settle_secs: float = 0.4,
    ) -> None:
        self._click_interval = click_interval
        self._drag_interval = drag_interval
        self._scroll_interval = scroll_interval
        self._type_interval = type_interval
        self._idle_interval = idle_interval
        self._settle_secs = settle_secs

        self._held_buttons: set[str] = set()
        self._drag_active: bool = False
        self._last_save_mono: float = 0.0
        self._settle_deadline: float = 0.0
        self._settle_pending: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_save(
        self, action_name: str, data: dict, mono: float,
    ) -> RetentionDecision:
        """Decide whether to save a screenshot for *action_name*.

        Parameters
        ----------
        action_name:
            The ``name`` field from the action event (e.g. ``"click"``,
            ``"move"``, ``"scroll"``, ``"press"``, ``"release"``).
        data:
            The full event ``data`` dict — used for ``pressed`` on clicks.
        mono:
            Current ``time.monotonic()`` value.
        """
        if action_name == "click":
            return self._on_click(data, mono)
        if action_name == "move":
            return self._on_move(mono)
        if action_name in _SCROLL_ACTIONS:
            return self._on_scroll(mono)
        if action_name in ("press", "release"):
            return self._on_key(mono)
        # Unknown action → fall through to existing gate
        return RetentionDecision.BASELINE

    def check_settle(self, mono: float) -> bool:
        """Return ``True`` exactly once when the settle deadline fires."""
        if self._settle_pending and mono >= self._settle_deadline:
            self._settle_pending = False
            return True
        return False

    def has_pending_settle(self) -> bool:
        """Whether a settle deadline is pending."""
        return self._settle_pending

    def time_until_settle(self, mono: float | None = None) -> float:
        """Seconds remaining until the settle deadline (may be negative).

        Parameters
        ----------
        mono:
            Current ``time.monotonic()`` value.  If *None*, uses an internal
            conservative estimate based on the last save time.
        """
        if not self._settle_pending:
            return float("inf")
        ref = mono if mono is not None else self._last_save_mono
        return self._settle_deadline - ref

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _on_click(self, data: dict, mono: float) -> RetentionDecision:
        pressed = data.get("pressed", False)
        button = data.get("button", "left")
        if pressed:
            self._held_buttons.add(button)
        else:
            self._held_buttons.discard(button)
            if self._drag_active and not self._held_buttons:
                # Drag ended — set settle deadline
                self._drag_active = False
                self._settle_deadline = mono + self._settle_secs
                self._settle_pending = True
        # Clicks always save
        self._last_save_mono = mono
        return RetentionDecision.SAVE

    def _on_move(self, mono: float) -> RetentionDecision:
        if self._held_buttons:
            # Dragging
            self._drag_active = True
            return self._check_interval(self._drag_interval, mono)
        # Idle mouse move
        return self._check_interval(self._idle_interval, mono)

    def _on_scroll(self, mono: float) -> RetentionDecision:
        # Reset settle deadline on every scroll (including momentum)
        self._settle_deadline = mono + self._settle_secs
        self._settle_pending = True
        return self._check_interval(self._scroll_interval, mono)

    def _on_key(self, mono: float) -> RetentionDecision:
        if self._drag_active:
            # Keyboard event during drag uses drag interval
            return self._check_interval(self._drag_interval, mono)
        return self._check_interval(self._type_interval, mono)

    def _check_interval(self, interval: float, mono: float) -> RetentionDecision:
        if interval <= 0.0 or (mono - self._last_save_mono) >= interval:
            self._last_save_mono = mono
            return RetentionDecision.BYPASS_DEDUP
        return RetentionDecision.SKIP
