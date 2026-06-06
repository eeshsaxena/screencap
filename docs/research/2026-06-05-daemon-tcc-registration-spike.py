#!/usr/bin/env python3
"""THROWAWAY spike tool for U7 (daemon TCC registration). NOT production code.

Delete this after the spike. It exercises each candidate registration mechanism
so a human can run it and then observe System Settings. See the runbook in
``docs/research/2026-06-05-daemon-tcc-registration-spike.md``.

Run from the repo root:

    PYTHONPATH=src python docs/research/2026-06-05-daemon-tcc-registration-spike.py <mechanism> [permission]

  mechanism:  request-api | real-capture | placement | check
  permission: screen_recording | accessibility | input_monitoring   (default: screen_recording)

It is deliberately self-contained and verbose: the *empirical* answer is what
appears in System Settings after you run it, not what this prints.
"""

from __future__ import annotations

import sys
import time

PERMS = ("screen_recording", "accessibility", "input_monitoring")


def _check() -> None:
    """Print the current (in-process) preflight state for all three perms."""
    from screencap.engine.platform.darwin import DarwinPlatform

    print("In-process preflight (this process's TCC subject):")
    print(f"  screen_recording  : {DarwinPlatform.is_screen_recording_enabled()}")
    print(f"  accessibility     : {DarwinPlatform.is_accessibility_enabled()}")
    print(f"  input_monitoring  : {DarwinPlatform.is_input_monitoring_enabled()}")
    print("\nFresh-subprocess probe (the daemon's detection path, U1):")
    from screencap.daemon import permission_probe

    print(f"  {permission_probe.probe_permissions()}")


def _request_api(permission: str) -> None:
    """Mechanism 1: bare request API. Research says this likely does NOT list a
    background helper as a toggleable entry."""
    from screencap.engine.platform.darwin import DarwinPlatform

    fns = {
        "screen_recording": DarwinPlatform.request_screen_recording_access,
        "accessibility": DarwinPlatform.request_accessibility_access,
        "input_monitoring": DarwinPlatform.request_input_monitoring_access,
    }
    print(f"[request-api] calling request for {permission} ...")
    result = fns[permission]()
    print(f"[request-api] returned {result!r}")
    print("Now open the matching pane and check for a toggleable helper entry.")


def _real_capture(permission: str) -> None:
    """Mechanism 2: a genuine capture touch. Research says initiating real
    capture is more likely to force the Screen Recording entry to appear than
    the bare request API. Only meaningful for screen_recording."""
    if permission != "screen_recording":
        print(f"[real-capture] no capture analog for {permission}; skipping.")
        print("  (Accessibility / Input Monitoring only have the request-API path.)")
        return

    print("[real-capture] attempting a real screen capture via Quartz ...")
    try:
        import Quartz

        # A real capture call. If Screen Recording is not granted this returns
        # nil / black frames, but the *attempt* is what may register the entry.
        display = Quartz.CGMainDisplayID()
        image = Quartz.CGDisplayCreateImage(display)
        if image is None:
            print("[real-capture] CGDisplayCreateImage returned None (no grant / black).")
        else:
            w = Quartz.CGImageGetWidth(image)
            h = Quartz.CGImageGetHeight(image)
            print(f"[real-capture] captured a {w}x{h} image (grant appears active).")

        # Also try a short CGDisplayStream, the path closest to what real
        # recorders use. Best-effort — APIs vary across macOS versions.
        try:
            queue = Quartz.dispatch_queue_create(b"spike.capture", None)

            def _handler(status, ts, frame, ref):  # noqa: ANN001
                return None

            stream = Quartz.CGDisplayStreamCreateWithDispatchQueue(
                display, 320, 240, 1111970369, None, queue, _handler
            )
            if stream is not None:
                Quartz.CGDisplayStreamStart(stream)
                time.sleep(1.0)
                Quartz.CGDisplayStreamStop(stream)
                print("[real-capture] CGDisplayStream start/stop completed.")
            else:
                print("[real-capture] CGDisplayStreamCreate* returned None.")
        except Exception as exc:  # noqa: BLE001
            print(f"[real-capture] CGDisplayStream path unavailable: {exc!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"[real-capture] Quartz capture failed: {exc!r}")
    print("Now open Screen Recording and check for a toggleable helper entry.")


def _placement() -> None:
    """Mechanism 3 (diagnostic): where does the daemon binary live + its argv?"""
    import sysconfig
    from pathlib import Path

    print("Daemon launch configuration (from the repo plist):")
    print("  BundleProgram = Contents/Resources/screencap-daemon-launcher")
    print("  -> NOT Contents/MacOS/ ; research says Resources placement is why")
    print("     a background helper fails to register a toggleable TCC entry.")
    print()
    print(f"sys.executable           : {sys.executable}")
    print(f"frozen (bundled binary?) : {getattr(sys, 'frozen', False)}")
    print(f"platform / base prefix   : {sys.platform} / {sysconfig.get_config_var('prefix')}")
    print()
    print("Against a built app, also run:")
    print('  codesign -dvvv "<App>.app/Contents/Resources/screencap-daemon-launcher"')
    print('  ls -la "<App>.app/Contents/MacOS" "<App>.app/Contents/Resources"')
    # If we appear to be inside an .app, show its layout.
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            print(f"\nDetected app bundle: {parent}")
            for sub in ("Contents/MacOS", "Contents/Resources"):
                d = parent / sub
                if d.exists():
                    names = sorted(p.name for p in d.iterdir())[:20]
                    print(f"  {sub}: {names}")
            break


def main(argv: list[str]) -> int:
    mechanism = argv[1] if len(argv) > 1 else "check"
    permission = argv[2] if len(argv) > 2 else "screen_recording"
    if permission not in PERMS:
        print(f"unknown permission {permission!r}; expected one of {PERMS}")
        return 2

    if mechanism == "check":
        _check()
    elif mechanism == "request-api":
        _request_api(permission)
    elif mechanism == "real-capture":
        _real_capture(permission)
    elif mechanism == "placement":
        _placement()
    else:
        print(f"unknown mechanism {mechanism!r}; expected request-api | real-capture | placement | check")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
