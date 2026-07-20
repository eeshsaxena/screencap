"""Minimal NSPanel test — bypasses menubar entirely.

Creates an NSApplication on the main thread, builds the prompt panel
with the same code as menubar.py:_maybe_show_next_prompt, shows it, and
runs the AppKit run loop for 20 seconds.

If the panel does NOT appear when you run this, the bug is in the
NSPanel construction code itself. If it DOES appear here but not in
the recording flow, the bug is in the menubar wiring.

Usage:
    python /Users/yogeshshahi/Documents/zkmail/screencap/scripts/screencap_panel_only.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(
    0, str(Path("/Users/yogeshshahi/Documents/zkmail/screencap/src")),
)

from AppKit import (  # noqa: E402
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSObject,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSTimer,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
)
from Foundation import NSMakePoint, NSMakeRect  # noqa: E402
from objc import super as objc_super  # noqa: A004,E402


class Driver(NSObject):
    def init(self):
        self = objc_super(Driver, self).init()
        if self is None:
            return None
        self._panel = None
        # Show the panel after 0.5s so AppKit is fully initialized
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "showPanel:", None, False,
        )
        # Quit after 20 seconds
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            20.0, self, "quit:", None, False,
        )
        return self

    def showPanel_(self, _timer):
        print(f"[{time.time():.3f}] Building NSPanel...")
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

        header = NSTextField.alloc().initWithFrame_(
            NSMakeRect(16, panel_h - 38, panel_w - 32, 20),
        )
        header.setStringValue_("New app detected during recording")
        header.setBezeled_(False)
        header.setDrawsBackground_(False)
        header.setEditable_(False)
        header.setSelectable_(False)
        header.setFont_(NSFont.boldSystemFontOfSize_(13.0))
        content.addSubview_(header)

        detail = NSTextField.alloc().initWithFrame_(
            NSMakeRect(16, panel_h - 64, panel_w - 32, 20),
        )
        detail.setStringValue_("Calculator  (com.apple.calculator)")
        detail.setBezeled_(False)
        detail.setDrawsBackground_(False)
        detail.setEditable_(False)
        detail.setSelectable_(False)
        detail.setFont_(NSFont.systemFontOfSize_(12.0))
        detail.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        content.addSubview_(detail)

        btn_w, btn_h = panel_w - 32, 28
        for label, y, action in [
            ("Always disable", 16 + btn_h * 2 + 8, "btnAlways:"),
            ("Disable for this recording", 16 + btn_h + 4, "btnOnce:"),
            ("Keep recording", 16, "btnKeep:"),
        ]:
            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(16, y, btn_w, btn_h),
            )
            btn.setTitle_(label)
            btn.setBezelStyle_(NSBezelStyleRounded)
            btn.setTarget_(self)
            btn.setAction_(action)
            if label == "Keep recording":
                btn.setKeyEquivalent_("\r")
            content.addSubview_(btn)

        try:
            screen_frame = NSScreen.mainScreen().visibleFrame()
            x = screen_frame.origin.x + screen_frame.size.width - panel_w - 12
            y = screen_frame.origin.y + screen_frame.size.height - panel_h - 12
            panel.setFrameOrigin_(NSMakePoint(x, y))
            print(
                f"[{time.time():.3f}] Panel positioned at ({x:.0f}, {y:.0f}) "
                f"on screen frame {screen_frame}"
            )
        except Exception as exc:
            print(f"  positioning failed: {exc}")

        self._panel = panel
        panel.orderFrontRegardless()
        print(f"[{time.time():.3f}] orderFrontRegardless() called")
        print(
            f"[{time.time():.3f}] panel.isVisible() = {panel.isVisible()}, "
            f"isOnActiveSpace() = {panel.isOnActiveSpace()}"
        )

    def btnAlways_(self, _sender):
        print(f"[{time.time():.3f}] CLICKED: Always disable")
        if self._panel:
            self._panel.orderOut_(None)
        NSApplication.sharedApplication().terminate_(None)

    def btnOnce_(self, _sender):
        print(f"[{time.time():.3f}] CLICKED: Disable for this recording")
        if self._panel:
            self._panel.orderOut_(None)
        NSApplication.sharedApplication().terminate_(None)

    def btnKeep_(self, _sender):
        print(f"[{time.time():.3f}] CLICKED: Keep recording")
        if self._panel:
            self._panel.orderOut_(None)
        NSApplication.sharedApplication().terminate_(None)

    def quit_(self, _timer):
        print(f"[{time.time():.3f}] Timeout — quitting")
        NSApplication.sharedApplication().terminate_(None)


def main() -> None:
    print(f"[{time.time():.3f}] Initializing NSApplication...")
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    driver = Driver.alloc().init()
    app.setDelegate_(driver)
    print(f"[{time.time():.3f}] Running app loop for 20 seconds...")
    app.run()
    print(f"[{time.time():.3f}] Done.")


if __name__ == "__main__":
    main()
