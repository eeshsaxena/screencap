"""macOS menu bar status item for active recordings.

Designed to run as a multiprocessing.Process target or standalone via:
    python -m screencap.menubar <parent_pid> <recording_name> <start_time> <state_file>

The subprocess shows a pulsing red dot with a live timer in the macOS menu
bar.  The dropdown contains a labelled editable recording name, elapsed time,
a live list of detected apps/browser tabs with inclusion/exclusion toggles,
and a "Stop Recording" button.  It communicates with the parent process via a
state file, a window feed queue, and an override queue.
"""

from __future__ import annotations

import math
import os
import queue as _queue_mod
import signal
import sys
import time
from pathlib import Path

from screencap.pidfile import _is_screencap_process
from screencap.privacy.actions import EXCLUDED_ACTION_VALUES, make_override_key
from screencap.privacy.context import PASSWORD_MANAGER_BUNDLES

# Animation / timing constants
_PULSE_FRAMES = 20        # frames per breathing cycle
_PULSE_INTERVAL = 0.08    # seconds between frames → 1.6 s / cycle
_SLOW_CHECK_MOD = 12      # heavy checks every 12 ticks → ~1 s
_TIME_UPDATE_MOD = 6       # timer text every 6 ticks → ~0.5 s
_STOP_ESCALATE_S = 8       # seconds before SIGTERM → SIGKILL
_MAX_DRAIN_PER_TICK = 20   # max window events to drain per slow tick

# State file protocol (shared with recorder.py / cli.py)
STATE_PROCESSING = "processing"
STATE_DONE = "done"
RENAME_FILENAME = ".menubar_rename"


def _run_menubar(
    parent_pid: int,
    recording_name: str,
    start_time: float,
    state_file: str,
    window_feed_q=None,
    override_q=None,
) -> None:
    """Main entry point — must run on the main thread of the subprocess."""
    # Reset inherited signal handlers from parent recorder process
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    try:
        from AppKit import (
            NSApplication,
            NSApplicationActivationPolicyAccessory,
            NSAttributedString,
            NSColor,
            NSFont,
            NSFontAttributeName,
            NSForegroundColorAttributeName,
            NSLineBreakByTruncatingTail,
            NSMenu,
            NSMenuItem,
            NSObject,
            NSStatusBar,
            NSTextField,
            NSTextFieldSquareBezel,
            NSTimer,
            NSVariableStatusItemLength,
        )
        from Foundation import NSMakeRect, NSMutableAttributedString
        from objc import super as objc_super  # noqa: A004
    except ImportError:
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

    def _build_bar_title(dot_color, elapsed_s):
        """Build an attributed string  ``● HH:MM:SS``  for the status item."""
        h, rem = divmod(int(elapsed_s), 3600)
        m, s = divmod(rem, 60)
        dot_part = NSAttributedString.alloc().initWithString_attributes_(
            "\u25cf ", {
                NSForegroundColorAttributeName: dot_color,
                NSFontAttributeName: _dot_font,
            },
        )
        time_part = NSAttributedString.alloc().initWithString_attributes_(
            f"{h:02d}:{m:02d}:{s:02d}", {
                NSForegroundColorAttributeName: _time_color,
                NSFontAttributeName: _time_font,
            },
        )
        result = NSMutableAttributedString.alloc().init()
        result.appendAttributedString_(dot_part)
        result.appendAttributedString_(time_part)
        return result

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

            # Detected Apps & Tabs header
            apps_header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Detected Apps & Tabs", None, "",
            )
            apps_header.setEnabled_(False)
            menu.addItem_(apps_header)

            # Track where new app items get inserted (before the stop separator)
            self._insert_index = menu.numberOfItems()

            menu.addItem_(NSMenuItem.separatorItem())

            # Stop Recording
            stop_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Stop Recording", "stopRecording:", "",
            )
            stop_item.setTarget_(self)
            menu.addItem_(stop_item)

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

            if not bundle_id:
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

            # Track whether the currently active app is excluded (for dot color)
            self._is_active_excluded = action in EXCLUDED_ACTION_VALUES

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

        # ---- Timer ----

        def tick_(self, timer):
            self._tick_count += 1
            elapsed = time.time() - self._start_time

            # ---- Drain window feed queue (every tick for instant updates) ----
            if self._window_feed_q is not None and not self._is_processing:
                drained = 0
                while drained < _MAX_DRAIN_PER_TICK:
                    try:
                        evt = self._window_feed_q.get_nowait()
                    except (_queue_mod.Empty, OSError):
                        break
                    self._process_window_event(evt)
                    drained += 1

            # ---- Pulse animation (every tick) ----
            if not self._is_processing:
                self._pulse_idx = (self._pulse_idx + 1) % _PULSE_FRAMES
                dots = _pulse_dots_gray if self._is_active_excluded else _pulse_dots_red
                self._status_item.button().setAttributedTitle_(
                    _build_bar_title(dots[self._pulse_idx], elapsed)
                )

            # ---- Update dropdown timer (~0.5 s) ----
            if self._tick_count % _TIME_UPDATE_MOD == 0 and not self._is_processing:
                h, rem = divmod(int(elapsed), 3600)
                m, s = divmod(rem, 60)
                self._time_item.setTitle_(f"Elapsed  {h:02d}:{m:02d}:{s:02d}")

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

            # Send override to recorder
            if self._override_q is not None:
                try:
                    self._override_q.put_nowait({
                        "key": key,
                        "bundle_id": info["bundle_id"],
                        "domain": info["domain"],
                        "action": new_action,
                    })
                except Exception:
                    pass

        def stopRecording_(self, sender):
            """Send SIGTERM to parent; escalates to SIGKILL after timeout."""
            try:
                if _is_screencap_process(self._parent_pid):
                    os.kill(self._parent_pid, signal.SIGTERM)
                    self._stop_sent_at = time.time()
            except (ProcessLookupError, PermissionError):
                pass

        def _cleanup(self):
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
