"""Standalone smoke test for the first-seen prompt panel.

Usage:
    python scripts/menubar_prompt_smoke.py

What it does:
    1. Spawns the menubar subprocess (same one used during recording)
    2. After 2 seconds, pushes a fake "new app detected" window event
       into the window_feed_q
    3. The menubar's normal first-seen path should pop the prompt panel
    4. Quit with Ctrl+C in this terminal, or click "Stop Recording" in
       the menu bar dropdown.

Expected: a floating panel appears in the top-right corner with three
buttons. Clicking any of them prints a message to stdout (because the
override is forwarded back via the queue and we drain+print here).
"""

from __future__ import annotations

import multiprocessing
import os
import queue as _queue_mod
import sys
import time
from pathlib import Path

# Make sure we're using the local source tree
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

from screencap.recorder import _spawn_menubar  # noqa: E402


def main() -> None:
    state_file = Path("/tmp/menubar_smoke_state")
    state_file.unlink(missing_ok=True)

    window_feed_q = multiprocessing.Queue()
    override_q = multiprocessing.Queue()

    print("Spawning menubar (red dot should appear in your menu bar)...")
    proc = _spawn_menubar(
        recording_name="smoke-test",
        start_time=time.time(),
        state_file=state_file,
        window_feed_q=window_feed_q,
        override_q=override_q,
        prompt_enabled=True,
    )
    if proc is None:
        print("FAIL: _spawn_menubar returned None")
        return

    print(f"Menubar PID: {proc.pid}")
    print("Sleeping 2 seconds so AppKit can initialize...")
    time.sleep(2.0)

    # Push a fake "new app detected" event. Should trigger the prompt.
    fake_event = {
        "app_name": "Calculator",
        "bundle_id": "com.apple.calculator",
        "domain": None,
        "action": "allow",
        "ts": time.time(),
    }
    print(f"\nPushing fake event: {fake_event}")
    print("→ A floating panel should appear in the top-right within ~1s.")
    print("  (one menubar tick is 0.08s; the queue is drained every tick)\n")
    window_feed_q.put_nowait(fake_event)

    # Push a second one for a "browser tab" key after a few seconds
    print("Will push a second fake event (browser tab) in 12 seconds")
    print("(after the first panel auto-dismisses).\n")

    print("Waiting for user clicks (Ctrl+C to quit)...")
    deadline = time.time() + 60.0
    second_pushed = False
    try:
        while time.time() < deadline:
            time.sleep(0.5)
            if not second_pushed and time.time() > deadline - 48:
                second_pushed = True
                browser_event = {
                    "app_name": "Google Chrome",
                    "bundle_id": "com.google.Chrome",
                    "domain": "github.com",
                    "action": "allow",
                    "ts": time.time(),
                }
                print(f"Pushing second event: {browser_event}")
                window_feed_q.put_nowait(browser_event)

            try:
                ov = override_q.get_nowait()
                print(f"  ← override returned: {ov}")
            except _queue_mod.Empty:
                pass

            if not proc.is_alive():
                print("Menubar process exited.")
                break
    except KeyboardInterrupt:
        print("\nCtrl+C — telling menubar to stop")
    finally:
        try:
            state_file.write_text("done")
        except Exception:
            pass
        time.sleep(0.5)
        if proc.is_alive():
            try:
                os.kill(proc.pid, 9)
            except Exception:
                pass
        proc.join(timeout=2.0)


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
