"""macOS menu bar status item for active recordings.

Designed to run as a multiprocessing.Process target or standalone via:
    python -m screencap.menubar <parent_pid> <recording_name> <start_time> <state_file>

The subprocess shows a pulsing red dot with a live timer in the macOS menu
bar.  The dropdown contains a labelled editable recording name, elapsed time,
a live list of detected apps/browser tabs with inclusion/exclusion toggles,
and a "Stop Recording" button.  It communicates with the parent process via a
state file, a window feed queue, and an override queue.

When ``prompt_enabled`` is True, the subprocess also shows a transient
non-activating NSPanel the first time a never-before-seen ``(app, domain)``
pair becomes the frontmost window during a recording. The panel offers
"Always disable", "Disable for this recording", and "Keep recording".
"""

from __future__ import annotations

import math
import os
import queue as _queue_mod
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from screencap.pidfile import _is_screencap_process
from screencap.privacy.actions import EXCLUDED_ACTION_VALUES, make_override_key
from screencap.privacy.classify import PASSWORD_MANAGER_BUNDLES

# Debug log — written by the menubar subprocess so we can post-mortem
# what the prompt path saw. Disabled by default; set
# SCREENCAP_MENUBAR_DEBUG=1 to enable.
# RUN-DIR boundary (SCR-236 R3): a diagnostic log, not recorded data — stays
# OUTSIDE the at-rest container. Do NOT route through config.get_data_root().
_DEBUG_LOG_PATH = Path.home() / ".screencap" / "menubar_debug.log"


def _dlog(msg: str) -> None:
    """Append a debug line to ``~/.screencap/menubar_debug.log`` if enabled."""
    if not os.environ.get("SCREENCAP_MENUBAR_DEBUG"):
        return
    try:
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a") as f:
            f.write(f"[{time.time():.3f}] [pid={os.getpid()}] {msg}\n")
    except Exception:
        pass


# Animation / timing constants
_PULSE_FRAMES = 20        # frames per breathing cycle
_PULSE_INTERVAL = 0.08    # seconds between frames → 1.6 s / cycle
_SLOW_CHECK_MOD = 12      # heavy checks every 12 ticks → ~1 s
_TIME_UPDATE_MOD = 6       # timer text every 6 ticks → ~0.5 s
_STOP_ESCALATE_S = 8       # seconds before SIGTERM → SIGKILL
_MAX_DRAIN_PER_TICK = 20   # max window events to drain per slow tick

# Status-bar glyphs. Filled = recording/processing; hollow = idle.
_GLYPH_FILLED = "\u25cf"   # ●
_GLYPH_HOLLOW = "\u25cb"   # ○

# Prompt timing / queue caps
_PROMPT_AUTO_DISMISS_S = 10.0    # seconds before auto-dismiss = "Keep recording"
_PROMPT_MAX_PENDING = 3          # FIFO depth; older entries dropped on overflow

# State file protocol (shared with recorder.py / cli.py)
STATE_PROCESSING = "processing"
STATE_DONE = "done"
RENAME_FILENAME = ".menubar_rename"

# Session-mode controller state strings. Duplicated here — rather than
# importing :class:`screencap.session.SessionState` — so the menubar
# subprocess doesn't pull in the 1k-line session module (and its
# multiprocessing / rich / OrderedDict imports) just to read an enum.
SESSION_STATE_IDLE = "idle"
SESSION_STATE_RECORDING = "recording"
SESSION_STATE_STOPPING = "stopping"


# ---------------------------------------------------------------------------
# PromptState — pure-Python state machine for the first-seen prompt queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptDecision:
    """A queued first-seen prompt waiting to be shown to the user."""

    bundle_id: str
    domain: str | None
    app_name: str

    @property
    def key(self) -> str:
        return make_override_key(self.bundle_id, self.domain)


@dataclass
class PromptState:
    """Pure-Python queue + dedup state for first-seen prompts.

    Decoupled from AppKit so it can be unit-tested without an
    NSApplication. The menubar delegate owns one instance and calls
    :meth:`should_prompt` from the window-event drain loop, then
    :meth:`enqueue` if the prompt is warranted, then :meth:`pop_next`
    when the panel slot opens up.
    """

    max_pending: int = _PROMPT_MAX_PENDING
    _prompted: set[str] = field(default_factory=set)
    _pending: deque[PromptDecision] = field(default_factory=deque)

    def should_prompt(
        self,
        bundle_id: str,
        domain: str | None,
        action: str,
        is_password_manager: bool,
        is_browser_header: bool,
    ) -> bool:
        """Decide whether a first-seen window event warrants a prompt.

        Skips:
        - Empty bundle IDs (defensive)
        - Password managers (already protected by the matrix EXCLUDE)
        - Browser-name headers (only the per-domain entries are useful)
        - Apps the policy already excludes (in EXCLUDED_ACTION_VALUES)
        - Keys already prompted in this session
        """
        if not bundle_id:
            return False
        if is_password_manager:
            return False
        if is_browser_header:
            return False
        if action in EXCLUDED_ACTION_VALUES:
            return False
        key = make_override_key(bundle_id, domain)
        if key in self._prompted:
            return False
        return True

    def enqueue(self, decision: PromptDecision) -> None:
        """Queue a prompt and mark its key as prompted (so we don't re-add).

        On overflow (more than ``max_pending`` queued), drops the oldest
        entry — the user has clearly switched apps too fast for the
        prompt to be useful for early entries.
        """
        self._prompted.add(decision.key)
        self._pending.append(decision)
        while len(self._pending) > self.max_pending:
            self._pending.popleft()

    def pop_next(
        self, current_frontmost_key: str | None = None,
    ) -> PromptDecision | None:
        """Return the next prompt to show, dropping stale entries.

        A queued prompt is "stale" if its key is no longer the frontmost
        window key — the user has already moved on, so prompting now
        would be confusing. Stale entries are silently discarded.

        Returns ``None`` if the queue is empty (after dropping stale).
        """
        while self._pending:
            decision = self._pending.popleft()
            if (
                current_frontmost_key is not None
                and decision.key != current_frontmost_key
            ):
                # Stale — user moved on. Drop and try the next entry.
                continue
            return decision
        return None

    def has_pending(self) -> bool:
        return bool(self._pending)

    def already_prompted(self, key: str) -> bool:
        return key in self._prompted


def _run_menubar(
    parent_pid: int,
    recording_name: str,
    start_time: float,
    state_file: str,
    window_feed_q=None,
    override_q=None,
    prompt_enabled: bool = True,
    disable_q=None,
    *,
    control_q=None,
    menubar_event_q=None,
    session_mode: bool = False,
    audio_enabled: bool = True,
) -> None:
    """Main entry point — must run on the main thread of the subprocess.

    Two operating modes:

    * **Legacy mode** (``session_mode=False``): the menubar is spawned
      per-recording by ``recorder.start_recording``. Window events come
      from ``window_feed_q``; overrides go to ``override_q``; stop is
      signalled by SIGTERM'ing the parent.

    * **Session mode** (``session_mode=True``): the menubar is spawned
      once by the :class:`SessionController` and persists across many
      recordings. ``control_q`` is the multiplexed controller→menubar
      channel (window events + state transitions + pp status).
      ``menubar_event_q`` is the menubar→controller reply channel (Start
      / Stop / Quit clicks, overrides, disables). The legacy ``*_q``
      params are ignored.
    """
    # Unit 6 (v1 transitional): when the SwiftUI app spawns ``screencap
    # start`` with ``SCREENCAP_PARENT=swiftui``, the SwiftUI shell owns
    # the menu bar. Returning here keeps the rest of the SessionController
    # lifecycle intact — best-effort writes to ``_control_q`` succeed
    # without a consumer. Standalone
    # CLI invocations (no env var) get the rumps menu bar as before.
    # Full deletion of this module + the cross-cutting refactor (IPC
    # cleanup, `.menubar_overrides.json` migration, [menubar] config
    # removal, scrub_worker rewiring) is a separate v2 cleanup ticket.
    # Note: this env var is a behavior-routing hint, NOT an
    # authentication mechanism — any local process that sets it gets
    # the no-op behavior. Do not use it for security decisions.
    if os.environ.get("SCREENCAP_PARENT") == "swiftui":
        try:
            from screencap._stderr_events import EVENT_MENUBAR_NEUTRALIZED_BY_ENV
            from screencap._stderr_events import emit_event as _emit_event
            _emit_event(EVENT_MENUBAR_NEUTRALIZED_BY_ENV, env="SCREENCAP_PARENT=swiftui")
        except Exception:
            pass
        return

    _dlog(
        f"_run_menubar START: parent_pid={parent_pid} name={recording_name!r} "
        f"prompt_enabled={prompt_enabled} "
        f"window_feed_q={window_feed_q is not None} "
        f"override_q={override_q is not None} "
        f"disable_q={disable_q is not None} "
        f"session_mode={session_mode} "
        f"control_q={control_q is not None} "
        f"menubar_event_q={menubar_event_q is not None} "
        f"audio_enabled={audio_enabled}"
    )

    # Reset inherited signal handlers from parent recorder process
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    try:
        from AppKit import (
            NSApplication,
            NSApplicationActivationPolicyAccessory,
            NSAttributedString,
            NSBackingStoreBuffered,
            NSBezelStyleRounded,
            NSButton,
            NSColor,
            NSFont,
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSLineBreakByTruncatingTail,
            NSMenu,
            NSMenuItem,
            NSObject,
            NSPanel,
            NSScreen,
            NSStatusBar,
            NSStatusWindowLevel,
            NSTextField,
            NSTextFieldSquareBezel,
            NSTimer,
            NSVariableStatusItemLength,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskNonactivatingPanel,
            NSWindowStyleMaskTitled,
            NSWindowStyleMaskUtilityWindow,
        )
        from Foundation import (
            NSMakePoint,
            NSMakeRect,
            NSMutableAttributedString,
        )
        from objc import super as objc_super  # noqa: A004
    except ImportError as exc:
        _dlog(f"_run_menubar: AppKit ImportError: {exc!r} — exiting")
        return  # AppKit unavailable — exit silently

    _state_path = Path(state_file)
    _rename_path = _state_path.parent / RENAME_FILENAME
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # ---- Pre-build pulse frames ----
    # Cosine-eased opacity: 1.0 → 0.15 → 1.0 over _PULSE_FRAMES steps
    _dot_font = NSFont.systemFontOfSize_(14.0)
    _time_font = NSFont.monospacedDigitSystemFontOfSize_weight_(12.0, 0.0)
    _time_color = NSColor.secondaryLabelColor()

    _pulse_dots_red = []
    _pulse_dots_gray = []
    for _i in range(_PULSE_FRAMES):
        _alpha = 0.15 + 0.85 * (
            0.5 + 0.5 * math.cos(2 * math.pi * _i / _PULSE_FRAMES)
        )
        _pulse_dots_red.append(
            NSColor.colorWithRed_green_blue_alpha_(1.0, 0.0, 0.0, _alpha)
        )
        _pulse_dots_gray.append(
            NSColor.colorWithRed_green_blue_alpha_(0.6, 0.6, 0.6, _alpha)
        )

    _orange_color = NSColor.orangeColor()

    # Colors for menu item marks (green ✓, red ✗)
    _green_color = NSColor.colorWithRed_green_blue_alpha_(0.2, 0.78, 0.35, 1.0)
    _red_color = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.27, 0.23, 1.0)
    _menu_font = NSFont.systemFontOfSize_(13.0)
    _label_color = NSColor.labelColor()

    def _build_menu_item_title(mark, mark_color, indent, label):
        """Build an attributed string with a colored mark and default-color label."""
        mark_part = NSAttributedString.alloc().initWithString_attributes_(
            f"{indent}{mark}  ", {
                NSForegroundColorAttributeName: mark_color,
                NSFontAttributeName: _menu_font,
            },
        )
        label_part = NSAttributedString.alloc().initWithString_attributes_(
            label, {
                NSForegroundColorAttributeName: _label_color,
                NSFontAttributeName: _menu_font,
            },
        )
        result = NSMutableAttributedString.alloc().init()
        result.appendAttributedString_(mark_part)
        result.appendAttributedString_(label_part)
        return result

    def _build_bar_title(dot_color, elapsed_s, *, glyph=_GLYPH_FILLED, show_time=True):
        """Build an attributed string for the status item.

        ``show_time=True`` → ``<glyph> HH:MM:SS`` (recording / processing).
        ``show_time=False`` → ``<glyph>`` alone (idle — no recording in progress).
        """
        dot_part = NSAttributedString.alloc().initWithString_attributes_(
            f"{glyph} " if show_time else glyph, {
                NSForegroundColorAttributeName: dot_color,
                NSFontAttributeName: _dot_font,
            },
        )
        result = NSMutableAttributedString.alloc().init()
        result.appendAttributedString_(dot_part)
        if show_time:
            h, rem = divmod(int(elapsed_s), 3600)
            m, s = divmod(rem, 60)
            time_part = NSAttributedString.alloc().initWithString_attributes_(
                f"{h:02d}:{m:02d}:{s:02d}", {
                    NSForegroundColorAttributeName: _time_color,
                    NSFontAttributeName: _time_font,
                },
            )
            result.appendAttributedString_(time_part)
        return result

    def _build_idle_bar_title():
        return _build_bar_title(
            _time_color, 0.0, glyph=_GLYPH_HOLLOW, show_time=False,
        )

    # ---- Delegates ----

    class NameFieldDelegate(NSObject):
        """Delegate for the editable name text field."""

        def controlTextDidEndEditing_(self, notification):
            text_field = notification.object()
            new_name = str(text_field.stringValue()).strip()
            if new_name and new_name != recording_name:
                try:
                    _rename_path.write_text(new_name)
                except Exception:
                    pass

    class MenuBarDelegate(NSObject):
        """NSApplication delegate that manages the status bar item."""

        def init(self):
            self = objc_super(MenuBarDelegate, self).init()
            if self is None:
                return None
            self._start_time = start_time
            self._parent_pid = parent_pid
            self._is_processing = False
            self._pulse_idx = 0
            self._stop_sent_at = 0.0  # timestamp when SIGTERM was sent

            # Menu bar IPC
            self._window_feed_q = window_feed_q
            self._override_q = override_q
            self._disable_q = disable_q  # retroactive scrub channel

            # Session-mode IPC (controller <-> menubar). In session mode
            # the controller owns the lifecycle and the menubar is long
            # lived; the three legacy queues above are ignored.
            self._session_mode = session_mode
            self._control_q = control_q
            self._menubar_event_q = menubar_event_q
            # Session state drives the top-slot button and menu layout.
            # "idle" = ▶ Start, "recording" = ■ Stop, "stopping" = hourglass.
            self._session_state = (
                SESSION_STATE_IDLE if session_mode else SESSION_STATE_RECORDING
            )
            self._pending_pp_count = 0
            self._recording_name = recording_name

            # First-seen prompt state
            self._prompt_enabled = prompt_enabled
            self._prompt_state = PromptState()
            self._active_panel = None
            self._active_panel_decision = None
            self._prompt_dismiss_timer = None
            self._current_frontmost_key = None

            # Audio default toggle state. Reflects the next recording's
            # audio setting; cannot change the current recording mid-stream
            # (engine audio process is spawned once at recorder startup).
            self._audio_enabled = audio_enabled
            self._audio_item = None  # set by _build_recording_menu

            # App/tab list state
            # key → {app_name, bundle_id, domain, action, label, menu_item,
            #         is_header, is_password_manager}
            self._detected_items = {}
            # Ordered list of keys for first-seen ordering
            self._item_order = []
            self._is_active_excluded = False
            # Track the last inserted menu item per browser for grouping.
            # Maps bundle_id → NSMenuItem (the most recently added domain
            # entry for that browser, or the header if no domains yet).
            self._browser_last_item = {}

            self._status_item = (
                NSStatusBar.systemStatusBar().statusItemWithLength_(
                    NSVariableStatusItemLength
                )
            )
            # Track whether the idle (hollow-circle, no-timer) title is
            # already painted — idle is static so we avoid redundant
            # AppKit setter calls every tick.
            self._idle_rendered = False
            # Last dropdown "Elapsed …" string, used to suppress same-value
            # setTitle_ calls across the ObjC bridge (fires twice/sec).
            self._last_elapsed_title = ""
            if self._session_mode and self._session_state == SESSION_STATE_IDLE:
                self._status_item.button().setAttributedTitle_(_build_idle_bar_title())
                self._idle_rendered = True
            else:
                self._status_item.button().setAttributedTitle_(
                    _build_bar_title(_pulse_dots_red[0], 0.0)
                )

            self._name_delegate = NameFieldDelegate.alloc().init()
            self._build_recording_menu()

            # Fast timer for smooth pulse; heavier work is throttled inside tick_
            self._timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    _PULSE_INTERVAL, self, "tick:", None, True
                )
            )
            self._tick_count = 0
            return self

        # ---- Menu builders ----

        def _build_recording_menu(self):
            menu = NSMenu.alloc().init()

            # Action button at the top (slot 0). Title + action swap on
            # state transitions so the user sees "Stop Recording" while
            # RECORDING and "▶ Start Recording" while IDLE — without
            # reflowing the menu. Stored as ``self._action_item`` so
            # ``_refresh_action_item`` can mutate it in place.
            if self._session_mode and self._session_state == SESSION_STATE_IDLE:
                action_title = "\u25b6 Start Recording"
                action_selector = "startRecording:"
            else:
                action_title = "Stop Recording"
                action_selector = "stopRecording:"
            action_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                action_title, action_selector, "",
            )
            action_item.setTarget_(self)
            menu.addItem_(action_item)
            self._action_item = action_item

            menu.addItem_(NSMenuItem.separatorItem())

            # Label for the name field
            label_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Name", None, "",
            )
            label_item.setEnabled_(False)
            menu.addItem_(label_item)

            # Editable name text field
            name_field = NSTextField.alloc().initWithFrame_(
                NSMakeRect(0, 0, 200, 24)
            )
            name_field.setStringValue_(recording_name)
            name_field.setEditable_(True)
            name_field.setBezeled_(True)
            name_field.setBezelStyle_(NSTextFieldSquareBezel)
            name_field.setFont_(NSFont.systemFontOfSize_(13.0))
            name_field.setPlaceholderString_("Recording name")
            name_field.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            name_field.setDelegate_(self._name_delegate)
            name_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "", None, "",
            )
            name_item.setView_(name_field)
            menu.addItem_(name_item)

            menu.addItem_(NSMenuItem.separatorItem())

            # Elapsed time in the dropdown
            self._time_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Elapsed  00:00:00", None, "",
            )
            self._time_item.setEnabled_(False)
            menu.addItem_(self._time_item)

            menu.addItem_(NSMenuItem.separatorItem())

            # Audio default toggle — affects the NEXT recording.  The
            # current recording's audio state is locked at recorder
            # startup (engine audio process is spawned once, no
            # pause/resume hook).  Clicking writes config.toml and, in
            # session mode, pushes an ``audio_toggle`` event to the
            # controller so the next ``_on_start_click`` honours the
            # new value without waiting for a fresh session.
            audio_state = "ON" if self._audio_enabled else "OFF"
            audio_mark = "\u2713" if self._audio_enabled else "\u2717"
            audio_color = _green_color if self._audio_enabled else _red_color
            audio_attr = _build_menu_item_title(
                audio_mark, audio_color, "",
                f"Audio (next recording): {audio_state}",
            )
            self._audio_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "", "toggleAudio:", "",
            )
            self._audio_item.setAttributedTitle_(audio_attr)
            self._audio_item.setTarget_(self)
            menu.addItem_(self._audio_item)

            menu.addItem_(NSMenuItem.separatorItem())

            # Detected Apps & Tabs header
            apps_header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Detected Apps & Tabs", None, "",
            )
            apps_header.setEnabled_(False)
            menu.addItem_(apps_header)

            # Track where new app items get inserted. Apps land BELOW
            # the header and ABOVE the trailing separator + Quit item,
            # so ``_insert_index`` captures the position right after
            # the header. Each insert increments ``_insert_index`` so
            # the trailing separator + Quit stay pinned to the bottom
            # of the menu as apps accumulate.
            self._insert_index = menu.numberOfItems()

            # Session-mode Quit item pinned to the bottom of the menu,
            # separated from the apps list. Only shown in session mode;
            # legacy one-shot mode has no persistent session to quit.
            if self._session_mode:
                menu.addItem_(NSMenuItem.separatorItem())
                quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    "Quit Screencap", "quitSession:", "",
                )
                quit_item.setTarget_(self)
                menu.addItem_(quit_item)
                self._quit_item = quit_item
            else:
                self._quit_item = None

            self._status_item.setMenu_(menu)

        def _build_processing_menu(self):
            menu = NSMenu.alloc().init()

            proc_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Processing\u2026", None, "",
            )
            proc_item.setEnabled_(False)
            menu.addItem_(proc_item)

            elapsed = time.time() - self._start_time
            h, rem = divmod(int(elapsed), 3600)
            m, s = divmod(rem, 60)
            dur_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"Recorded {h:02d}:{m:02d}:{s:02d}", None, "",
            )
            dur_item.setEnabled_(False)
            menu.addItem_(dur_item)

            self._status_item.setMenu_(menu)

        # ---- App/tab list management ----

        def _process_window_event(self, evt):
            """Process a single window event from the feed queue."""
            bundle_id = evt.get("bundle_id", "")
            domain = evt.get("domain")
            app_name = evt.get("app_name", "") or bundle_id.rsplit(".", 1)[-1]
            action = evt.get("action", "allow")

            _dlog(
                f"window_evt: bundle={bundle_id!r} domain={domain!r} "
                f"app={app_name!r} action={action!r}"
            )

            if not bundle_id:
                _dlog("  → skipped (empty bundle_id)")
                return

            key = make_override_key(bundle_id, domain)
            is_pw = bundle_id in PASSWORD_MANAGER_BUNDLES

            if key in self._detected_items:
                info = self._detected_items[key]
                old_action = info["action"]
                if old_action != action and not info.get("user_toggled"):
                    info["action"] = action
                    self._update_item_title(info)
            else:
                if domain:
                    label = domain
                    indent = "    "
                    if bundle_id not in self._detected_items:
                        self._add_browser_header(bundle_id, app_name)
                else:
                    label = app_name
                    indent = ""

                menu = self._status_item.menu()
                excluded = action in EXCLUDED_ACTION_VALUES

                if is_pw:
                    attr_title = _build_menu_item_title(
                        "\u2717", _red_color, indent, f"{label} (protected)",
                    )
                    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        "", None, "",
                    )
                    item.setAttributedTitle_(attr_title)
                    item.setEnabled_(False)
                else:
                    mark = "\u2713" if not excluded else "\u2717"
                    color = _green_color if not excluded else _red_color
                    attr_title = _build_menu_item_title(
                        mark, color, indent, label,
                    )
                    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        "", "toggleApp:", "",
                    )
                    item.setAttributedTitle_(attr_title)
                    item.setTarget_(self)
                    item.setRepresentedObject_(key)

                # Insert position: browser domains go right after their
                # browser's last item; everything else appends at the end.
                if domain and bundle_id in self._browser_last_item:
                    last = self._browser_last_item[bundle_id]
                    insert_at = menu.indexOfItem_(last) + 1
                else:
                    insert_at = self._insert_index
                menu.insertItem_atIndex_(item, insert_at)
                self._insert_index += 1

                if domain:
                    self._browser_last_item[bundle_id] = item

                self._detected_items[key] = {
                    "app_name": app_name,
                    "bundle_id": bundle_id,
                    "domain": domain,
                    "action": action,
                    "label": label,
                    "menu_item": item,
                    "is_header": False,
                    "is_password_manager": is_pw,
                    "user_toggled": False,
                }
                self._item_order.append(key)

                # CRITICAL: update _current_frontmost_key BEFORE the prompt
                # logic. _maybe_show_next_prompt's pop_next() uses this for
                # the staleness check; if we don't update first, the
                # just-enqueued decision is compared against the *previous*
                # event's key and silently dropped as stale.
                self._current_frontmost_key = key

                # First-seen prompt: ask the user whether to disable
                # recording for this app/site. Only fires for genuinely
                # new keys (this branch is the dedup gate).
                _dlog(
                    f"  first_seen branch for key={key!r}; "
                    f"prompt_enabled={self._prompt_enabled} is_pw={is_pw}"
                )
                if self._prompt_enabled:
                    decided = self._prompt_state.should_prompt(
                        bundle_id, domain, action,
                        is_password_manager=is_pw,
                        is_browser_header=False,
                    )
                    _dlog(f"  should_prompt returned {decided}")
                    if decided:
                        self._prompt_state.enqueue(PromptDecision(
                            bundle_id=bundle_id, domain=domain, app_name=app_name,
                        ))
                        _dlog(
                            f"  enqueued; calling _maybe_show_next_prompt "
                            f"(active_panel={self._active_panel is not None})"
                        )
                        try:
                            self._maybe_show_next_prompt()
                        except Exception as exc:  # noqa: BLE001
                            _dlog(f"  EXCEPTION in _maybe_show_next_prompt: {exc!r}")

            # Track whether the currently active app is excluded (for dot color).
            # Note: _current_frontmost_key was already set above (in the
            # first-seen branch) or stays at its previous value (in the
            # already-seen branch — which is also correct because the user
            # is on the same app).
            self._is_active_excluded = action in EXCLUDED_ACTION_VALUES
            self._current_frontmost_key = key

        def _add_browser_header(self, bundle_id, app_name):
            """Add a non-clickable browser name header."""
            menu = self._status_item.menu()
            header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"  {app_name}", None, "",
            )
            header.setEnabled_(False)
            menu.insertItem_atIndex_(header, self._insert_index)
            self._insert_index += 1
            self._browser_last_item[bundle_id] = header

            self._detected_items[bundle_id] = {
                "app_name": app_name,
                "bundle_id": bundle_id,
                "domain": None,
                "action": "header",
                "label": app_name,
                "menu_item": header,
                "is_header": True,
                "is_password_manager": False,
                "user_toggled": False,
            }
            self._item_order.append(bundle_id)

        def _update_item_title(self, info):
            """Update an existing menu item's title to reflect current action."""
            item = info.get("menu_item")
            if item is None or info.get("is_header") or info.get("is_password_manager"):
                return
            indent = "    " if info.get("domain") else ""
            excluded = info["action"] in EXCLUDED_ACTION_VALUES
            mark = "\u2717" if excluded else "\u2713"
            color = _red_color if excluded else _green_color
            item.setAttributedTitle_(
                _build_menu_item_title(mark, color, indent, info["label"])
            )

        # ---- Controller message handling (session mode) ----

        def _handle_control_msg(self, msg):
            """Dispatch a single message drained from ``control_q``."""
            if not isinstance(msg, dict):
                return
            mtype = msg.get("type")
            if mtype == "window_event":
                data = msg.get("data")
                if isinstance(data, dict):
                    self._process_window_event(data)
                return
            if mtype == "state":
                new_state = msg.get("state", "idle")
                self._session_state = new_state
                if "name" in msg and msg.get("name"):
                    self._recording_name = msg["name"]
                if "start_time" in msg and msg.get("start_time"):
                    try:
                        self._start_time = float(msg["start_time"])
                    except (TypeError, ValueError):
                        pass
                if "pending" in msg:
                    try:
                        self._pending_pp_count = int(msg["pending"])
                    except (TypeError, ValueError):
                        pass
                self._refresh_action_item()
                return
            if mtype in ("pp_status", "pp_done"):
                try:
                    self._pending_pp_count = int(msg.get("pending", 0))
                except (TypeError, ValueError):
                    self._pending_pp_count = 0
                self._refresh_action_item()
                return
            if mtype == "session_reset":
                # Clear detected app list + first-seen dedup so the next
                # recording starts with a fresh Apps/Tabs list.
                try:
                    menu = self._status_item.menu()
                    for info in list(self._detected_items.values()):
                        item = info.get("menu_item")
                        if item is not None:
                            idx = menu.indexOfItem_(item)
                            if idx != -1:
                                menu.removeItemAtIndex_(idx)
                                self._insert_index -= 1
                    self._detected_items.clear()
                    self._item_order.clear()
                    self._browser_last_item.clear()
                except Exception:
                    pass
                self._prompt_state = PromptState()
                return
            if mtype == "shutdown":
                self._cleanup()
                return

        def _refresh_action_item(self):
            """Swap the action item's title + selector for the current state.

            No-ops when neither the state nor the pending count has
            changed since the last call, so AppKit setters don't churn
            on every duplicate ``state`` / ``pp_status`` message.
            """
            item = getattr(self, "_action_item", None)
            if item is None:
                return
            rendered_key = (self._session_state, self._pending_pp_count)
            if rendered_key == getattr(self, "_action_item_rendered_key", None):
                return
            self._action_item_rendered_key = rendered_key

            state = self._session_state
            if state == SESSION_STATE_IDLE:
                title = "\u25b6 Start Recording"
                selector = "startRecording:"
                if self._pending_pp_count > 0:
                    title = (
                        f"{title}   (processing {self._pending_pp_count}…)"
                    )
            elif state == SESSION_STATE_STOPPING:
                title = "Stopping…"
                selector = None
            else:  # SESSION_STATE_RECORDING
                title = "\u25a0 Stop Recording"
                selector = "stopRecording:"
            try:
                item.setTitle_(title)
                if selector is None:
                    item.setAction_(None)
                    item.setEnabled_(False)
                else:
                    item.setAction_(selector)
                    item.setTarget_(self)
                    item.setEnabled_(True)
            except Exception:
                pass

        # ---- Timer ----

        def tick_(self, timer):
            self._tick_count += 1
            elapsed = time.time() - self._start_time

            # ---- Drain controller channel (session mode) ----
            if self._session_mode and self._control_q is not None:
                drained = 0
                while drained < _MAX_DRAIN_PER_TICK:
                    try:
                        msg = self._control_q.get_nowait()
                    except (_queue_mod.Empty, OSError):
                        break
                    self._handle_control_msg(msg)
                    drained += 1

            # ---- Drain window feed queue (legacy mode) ----
            if (
                not self._session_mode
                and self._window_feed_q is not None
                and not self._is_processing
            ):
                drained = 0
                while drained < _MAX_DRAIN_PER_TICK:
                    try:
                        evt = self._window_feed_q.get_nowait()
                    except (_queue_mod.Empty, OSError):
                        break
                    self._process_window_event(evt)
                    drained += 1
                if drained > 0:
                    _dlog(f"tick: drained {drained} window event(s)")

            # In session mode the "elapsed" timer only runs during the
            # RECORDING state — in IDLE we freeze it at 0 so the menubar
            # reflects "no capture in progress" until the next Start.
            _not_recording = (
                self._session_mode
                and self._session_state != SESSION_STATE_RECORDING
            )
            if _not_recording:
                elapsed = 0.0

            # ---- Pulse animation (every tick) ----
            if not self._is_processing:
                self._pulse_idx = (self._pulse_idx + 1) % _PULSE_FRAMES
                if _not_recording:
                    # Idle: static hollow circle, no timer — clearly
                    # "not recording, click to start". Painted once per
                    # transition to avoid churning AppKit every tick.
                    if not self._idle_rendered:
                        self._status_item.button().setAttributedTitle_(
                            _build_idle_bar_title()
                        )
                        self._idle_rendered = True
                else:
                    self._idle_rendered = False
                    dots = _pulse_dots_gray if self._is_active_excluded else _pulse_dots_red
                    self._status_item.button().setAttributedTitle_(
                        _build_bar_title(dots[self._pulse_idx], elapsed)
                    )

            # ---- Update dropdown timer (~0.5 s) ----
            if self._tick_count % _TIME_UPDATE_MOD == 0 and not self._is_processing:
                h, rem = divmod(int(elapsed), 3600)
                m, s = divmod(rem, 60)
                title = f"Elapsed  {h:02d}:{m:02d}:{s:02d}"
                if title != self._last_elapsed_title:
                    self._time_item.setTitle_(title)
                    self._last_elapsed_title = title

            # ---- Heavy checks (~1 s) ----
            if self._tick_count % _SLOW_CHECK_MOD != 0:
                return

            # State file transitions
            try:
                if _state_path.exists():
                    state = _state_path.read_text().strip()
                    if state == STATE_DONE:
                        self._cleanup()
                        return
                    if state == STATE_PROCESSING and not self._is_processing:
                        self._is_processing = True
                        # Parent is shutting down gracefully — cancel any
                        # SIGKILL escalation so post-processing can finish.
                        self._stop_sent_at = 0.0
                        self._status_item.button().setAttributedTitle_(
                            _build_bar_title(_orange_color, elapsed)
                        )
                        self._build_processing_menu()
            except Exception:
                pass

            # Escalate to SIGKILL only if SIGTERM was sent AND the parent
            # has NOT transitioned to "processing" (i.e. truly stuck).
            if self._stop_sent_at > 0:
                if time.time() - self._stop_sent_at > _STOP_ESCALATE_S:
                    try:
                        os.kill(self._parent_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    self._stop_sent_at = 0.0

            # Parent alive check
            try:
                os.kill(self._parent_pid, 0)
                if not _is_screencap_process(self._parent_pid):
                    self._cleanup()
            except (ProcessLookupError, PermissionError):
                self._cleanup()

        # ---- Actions ----

        def toggleApp_(self, sender):
            """Toggle inclusion/exclusion for an app or browser tab."""
            key = sender.representedObject()
            if key not in self._detected_items:
                return

            info = self._detected_items[key]
            if info.get("is_password_manager") or info.get("is_header"):
                return

            if info["action"] in EXCLUDED_ACTION_VALUES:
                new_action = "allow"
            else:
                new_action = "exclude"

            info["action"] = new_action
            info["user_toggled"] = True
            self._update_item_title(info)

            override_payload = {
                "key": key,
                "bundle_id": info["bundle_id"],
                "domain": info["domain"],
                "action": new_action,
            }

            # Send override to recorder (forward gating). In session mode
            # the controller owns the per-worker override_q, so we
            # publish onto menubar_event_q and let it forward.
            if self._session_mode and self._menubar_event_q is not None:
                try:
                    self._menubar_event_q.put_nowait({
                        "type": "override",
                        "data": override_payload,
                    })
                except Exception:
                    pass
            elif self._override_q is not None:
                try:
                    self._override_q.put_nowait(override_payload)
                except Exception:
                    pass

            # Re-enable does NOT restore deleted rows — the disable
            # channel is one-way: forward gating reverses, scrubbed
            # history does not.
            if new_action in EXCLUDED_ACTION_VALUES:
                self._emit_disable_message(
                    bundle_id=info["bundle_id"],
                    app_name=info.get("app_name"),
                    domain=info["domain"],
                    source="menubar",
                )

        def _update_audio_item_title(self):
            """Rebuild the audio item's attributed title from self._audio_enabled."""
            if self._audio_item is None:
                return
            mark = "\u2713" if self._audio_enabled else "\u2717"
            color = _green_color if self._audio_enabled else _red_color
            state = "ON" if self._audio_enabled else "OFF"
            attr = _build_menu_item_title(
                mark, color, "",
                f"Audio (next recording): {state}",
            )
            try:
                self._audio_item.setAttributedTitle_(attr)
            except Exception:
                pass

        def toggleAudio_(self, sender):
            """Flip the next-recording audio default.

            Writes ``config.toml`` so the change survives across
            ``screencap start`` invocations.  In session mode also pushes
            an ``audio_toggle`` event to the controller via
            ``menubar_event_q`` so the new value applies to the next
            recording started within the current session (which reads
            from ``SessionController._audio_effective``, not
            ``config.toml``).
            """
            new_value = not self._audio_enabled
            try:
                from screencap.config import set_audio_default
                set_audio_default(new_value)
            except Exception as exc:
                _dlog(f"toggleAudio_: set_audio_default failed: {exc!r}")
                return  # keep delegate state consistent with disk

            self._audio_enabled = new_value
            self._update_audio_item_title()

            if self._session_mode and self._menubar_event_q is not None:
                try:
                    self._menubar_event_q.put_nowait({
                        "type": "audio_toggle",
                        "value": new_value,
                    })
                except Exception as exc:
                    _dlog(f"toggleAudio_: menubar_event_q put failed: {exc!r}")

        # ---- First-seen prompt ----

        def _maybe_show_next_prompt(self):
            """Pop the next pending prompt and show it as a non-activating panel.

            No-op if a panel is already visible (one at a time) or the
            queue is empty / all entries are stale.
            """
            _dlog(
                f"_maybe_show_next_prompt: active_panel="
                f"{self._active_panel is not None} "
                f"frontmost_key={self._current_frontmost_key!r}"
            )
            if self._active_panel is not None:
                _dlog("  → already showing a panel, skip")
                return
            decision = self._prompt_state.pop_next(self._current_frontmost_key)
            _dlog(f"  pop_next returned: {decision!r}")
            if decision is None:
                return
            _dlog(f"  building NSPanel for {decision.bundle_id!r}")

            # Build the panel content
            panel_w, panel_h = 340, 170
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, panel_w, panel_h),
                NSWindowStyleMaskNonactivatingPanel
                | NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskUtilityWindow,
                NSBackingStoreBuffered,
                False,
            )
            panel.setLevel_(NSStatusWindowLevel)
            panel.setHidesOnDeactivate_(False)
            panel.setReleasedWhenClosed_(False)
            panel.setTitle_("Screencap")
            panel.setFloatingPanel_(True)
            panel.setBecomesKeyOnlyIfNeeded_(True)

            content = panel.contentView()

            # Header label
            header = NSTextField.alloc().initWithFrame_(
                NSMakeRect(16, panel_h - 38, panel_w - 32, 20),
            )
            header.setStringValue_("New app/website detected during recording")
            header.setBezeled_(False)
            header.setDrawsBackground_(False)
            header.setEditable_(False)
            header.setSelectable_(False)
            header.setFont_(NSFont.boldSystemFontOfSize_(13.0))
            content.addSubview_(header)

            # Detail label — app name (and domain if browser tab)
            if decision.domain:
                detail_text = (
                    f"{decision.domain}  ({decision.app_name})"
                )
            else:
                detail_text = (
                    f"{decision.app_name}  ({decision.bundle_id})"
                )
            detail = NSTextField.alloc().initWithFrame_(
                NSMakeRect(16, panel_h - 64, panel_w - 32, 20),
            )
            detail.setStringValue_(detail_text)
            detail.setBezeled_(False)
            detail.setDrawsBackground_(False)
            detail.setEditable_(False)
            detail.setSelectable_(False)
            detail.setFont_(NSFont.systemFontOfSize_(12.0))
            detail.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            content.addSubview_(detail)

            # Buttons — stacked vertically, "Keep recording" at bottom (default)
            btn_w, btn_h = panel_w - 32, 28
            y_top = 16 + btn_h * 2 + 8
            y_mid = 16 + btn_h + 4
            y_bot = 16

            always_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(16, y_top, btn_w, btn_h),
            )
            always_btn.setTitle_("Always disable")
            always_btn.setBezelStyle_(NSBezelStyleRounded)
            always_btn.setTarget_(self)
            always_btn.setAction_("promptDisableAlways:")
            content.addSubview_(always_btn)

            once_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(16, y_mid, btn_w, btn_h),
            )
            once_btn.setTitle_("Disable for this recording")
            once_btn.setBezelStyle_(NSBezelStyleRounded)
            once_btn.setTarget_(self)
            once_btn.setAction_("promptDisableOnce:")
            content.addSubview_(once_btn)

            keep_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(16, y_bot, btn_w, btn_h),
            )
            keep_btn.setTitle_("Keep recording")
            keep_btn.setBezelStyle_(NSBezelStyleRounded)
            keep_btn.setTarget_(self)
            keep_btn.setAction_("promptKeep:")
            keep_btn.setKeyEquivalent_("\r")  # Return = default action
            content.addSubview_(keep_btn)

            # Position near the menu bar status item, top-right of screen
            try:
                screen_frame = NSScreen.mainScreen().visibleFrame()
                x = (
                    screen_frame.origin.x
                    + screen_frame.size.width
                    - panel_w - 12
                )
                y = (
                    screen_frame.origin.y
                    + screen_frame.size.height
                    - panel_h - 12
                )
                panel.setFrameOrigin_(NSMakePoint(x, y))
            except Exception:
                pass  # Use default position on positioning failure

            self._active_panel = panel
            self._active_panel_decision = decision

            panel.orderFrontRegardless()
            _dlog(
                f"  orderFrontRegardless called; isVisible="
                f"{bool(panel.isVisible())} isOnActiveSpace="
                f"{bool(panel.isOnActiveSpace())}"
            )

            # Auto-dismiss after _PROMPT_AUTO_DISMISS_S = "Keep recording"
            self._prompt_dismiss_timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    _PROMPT_AUTO_DISMISS_S, self, "promptAutoDismiss:",
                    None, False,
                )
            )

        def _emit_disable_message(
            self, *, bundle_id, app_name, domain, source,
        ):
            """Publish a retroactive-scrub job to the recorder side.

            In session mode this is forwarded via ``menubar_event_q``;
            the controller routes it to the active recording worker's
            ``disable_q``. In legacy mode it goes directly onto the
            worker's ``disable_q``. Best-effort either way.
            """
            payload = {
                "kind": "domain" if domain else "app",
                "bundle_id": bundle_id,
                "app_name": app_name,
                "root_domain": domain,
                "ts_unix": time.time(),
                "source": source,
            }
            if self._session_mode and self._menubar_event_q is not None:
                try:
                    self._menubar_event_q.put_nowait({
                        "type": "disable",
                        "data": payload,
                    })
                except Exception:
                    pass
                return
            if self._disable_q is None:
                return
            try:
                self._disable_q.put_nowait(payload)
            except Exception:
                pass

        def _send_disable_override(self):
            """Push an exclude override for the active panel's decision."""
            d = self._active_panel_decision
            if d is None or self._override_q is None:
                return
            key = make_override_key(d.bundle_id, d.domain)
            try:
                self._override_q.put_nowait({
                    "key": key,
                    "bundle_id": d.bundle_id,
                    "domain": d.domain,
                    "action": "exclude",
                })
            except Exception:
                return
            self._emit_disable_message(
                bundle_id=d.bundle_id,
                app_name=d.app_name,
                domain=d.domain,
                source="menubar_prompt",
            )
            # Reflect in the existing menu item, if present
            info = self._detected_items.get(key)
            if info is not None and not info.get("is_password_manager") \
                    and not info.get("is_header"):
                info["action"] = "exclude"
                info["user_toggled"] = True
                self._update_item_title(info)

        def _dismiss_active_panel(self):
            """Tear down the visible panel (if any) and show the next one."""
            if self._prompt_dismiss_timer is not None:
                try:
                    self._prompt_dismiss_timer.invalidate()
                except Exception:
                    pass
                self._prompt_dismiss_timer = None
            if self._active_panel is not None:
                try:
                    self._active_panel.orderOut_(None)
                except Exception:
                    pass
                self._active_panel = None
                self._active_panel_decision = None
            # Show the next queued prompt, if any
            self._maybe_show_next_prompt()

        def promptKeep_(self, sender):
            """User chose to keep recording — no override, no persist."""
            self._dismiss_active_panel()

        def promptDisableOnce_(self, sender):
            """User chose to disable for this recording only."""
            self._send_disable_override()
            self._dismiss_active_panel()

        def promptDisableAlways_(self, sender):
            """User chose to disable AND persist to ~/.screencap/config.toml."""
            d = self._active_panel_decision
            self._send_disable_override()
            if d is not None:
                try:
                    from screencap.enforcement.persistence import persist_disable
                    persist_disable(d.bundle_id, d.domain)
                except Exception:
                    pass  # Best-effort; per-recording override still applied
            self._dismiss_active_panel()

        def promptAutoDismiss_(self, timer):
            """NSTimer callback — equivalent to "Keep recording"."""
            self._dismiss_active_panel()

        def stopRecording_(self, sender):
            """Request stop.

            Session mode: publish ``stop_click`` on ``menubar_event_q``
            so the controller can orchestrate a clean stop + transition
            to Idle. Legacy mode: SIGTERM the parent (the old one-shot
            recorder path).
            """
            if self._session_mode and self._menubar_event_q is not None:
                try:
                    self._menubar_event_q.put_nowait({"type": "stop_click"})
                except Exception:
                    pass
                return
            try:
                if _is_screencap_process(self._parent_pid):
                    os.kill(self._parent_pid, signal.SIGTERM)
                    self._stop_sent_at = time.time()
            except (ProcessLookupError, PermissionError):
                pass

        def startRecording_(self, sender):
            """Session mode: ask the controller to begin a new recording."""
            if not self._session_mode or self._menubar_event_q is None:
                return
            try:
                self._menubar_event_q.put_nowait({"type": "start_click"})
            except Exception:
                pass

        def quitSession_(self, sender):
            """Session mode: ask the controller to tear down the session."""
            if not self._session_mode or self._menubar_event_q is None:
                return
            try:
                self._menubar_event_q.put_nowait({"type": "quit_click"})
            except Exception:
                pass

        def _cleanup(self):
            # Dismiss any visible prompt panel before tearing down AppKit
            if self._active_panel is not None or self._prompt_dismiss_timer is not None:
                try:
                    self._dismiss_active_panel()
                except Exception:
                    pass
            NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            if self._timer:
                self._timer.invalidate()
                self._timer = None
            try:
                _state_path.unlink(missing_ok=True)
            except Exception:
                pass
            NSApplication.sharedApplication().terminate_(None)

    delegate = MenuBarDelegate.alloc().init()
    if delegate is None:
        return
    app.setDelegate_(delegate)
    app.run()


def main() -> None:
    """CLI entry point for ``python -m screencap.menubar``."""
    if len(sys.argv) != 5:
        print(
            "Usage: python -m screencap.menubar "
            "<parent_pid> <name> <start_time> <state_file>",
            file=sys.stderr,
        )
        sys.exit(1)
    _run_menubar(
        int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]
    )


if __name__ == "__main__":
    main()
