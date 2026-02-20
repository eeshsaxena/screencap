"""Collect system metrics for each recording."""

from __future__ import annotations

import json
import platform
import plistlib
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import mss
import psutil

from screencap import __version__

METRICS_FILENAME = "system_metrics.json"
SCHEMA_VERSION = 2


def _parse_plist_array(text: str) -> list[str] | None:
    """Parse a macOS plist text-format array into a Python list."""
    lines = text.strip().splitlines()
    items = []
    for line in lines:
        line = line.strip().rstrip(",")
        if line.startswith("(") or line.startswith(")"):
            continue
        # Strip surrounding quotes
        val = line.strip('" ')
        if val:
            items.append(val)
    return items or None


def _collect_locale() -> dict:
    """Gather macOS locale, language, keyboard, and regional settings."""
    locale_info: dict = {}

    # System locale (e.g. "en_US")
    try:
        result = subprocess.run(
            ["defaults", "read", "NSGlobalDomain", "AppleLocale"],
            capture_output=True, text=True, timeout=5,
        )
        raw = result.stdout.strip()
        # Strip currency suffix for the locale field (e.g. "en_US@currency=EUR" -> "en_US")
        locale_info["system_locale"] = raw.split("@")[0] if raw else None
    except Exception:
        locale_info["system_locale"] = None

    # Preferred languages (e.g. ["en-US", "pt-BR"])
    try:
        result = subprocess.run(
            ["defaults", "read", "NSGlobalDomain", "AppleLanguages"],
            capture_output=True, text=True, timeout=5,
        )
        locale_info["preferred_languages"] = _parse_plist_array(result.stdout)
    except Exception:
        locale_info["preferred_languages"] = None

    # Keyboard layout (e.g. "com.apple.keylayout.US")
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.HIToolbox",
             "AppleCurrentKeyboardLayoutInputSourceID"],
            capture_output=True, text=True, timeout=5,
        )
        val = result.stdout.strip()
        locale_info["keyboard_layout"] = val or None
    except Exception:
        locale_info["keyboard_layout"] = None

    # Input sources
    try:
        result = subprocess.run(
            ["defaults", "export", "com.apple.HIToolbox", "-"],
            capture_output=True, timeout=5,
        )
        plist_data = plistlib.loads(result.stdout)
        sources = plist_data.get("AppleEnabledInputSources", [])
        locale_info["input_sources"] = [
            s.get("KeyboardLayout Name") or s.get("Input Mode") or s.get("Bundle ID", "")
            for s in sources
        ] or None
    except Exception:
        locale_info["input_sources"] = None

    # Timezone (e.g. "America/New_York")
    try:
        tz = datetime.now().astimezone().tzinfo
        tz_name = getattr(tz, "key", None) or str(tz)
        locale_info["timezone"] = tz_name
    except Exception:
        locale_info["timezone"] = None

    # Timezone offset (e.g. "-05:00")
    try:
        now = datetime.now().astimezone()
        offset = now.strftime("%z")  # e.g. "-0500"
        locale_info["timezone_offset"] = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    except Exception:
        locale_info["timezone_offset"] = None

    # Date format (short, e.g. "M/d/yy")
    try:
        result = subprocess.run(
            ["defaults", "read", "NSGlobalDomain", "AppleICUDateFormatStrings"],
            capture_output=True, text=True, timeout=5,
        )
        # Output is a plist dict; extract key "1" (short format)
        for line in result.stdout.splitlines():
            if line.strip().startswith("1 ="):
                locale_info["date_format"] = line.split("=", 1)[1].strip().strip('";')
                break
        else:
            locale_info["date_format"] = None
    except Exception:
        locale_info["date_format"] = None

    # Number format (decimal and grouping separators)
    try:
        result = subprocess.run(
            ["defaults", "read", "NSGlobalDomain", "AppleICUNumberSymbols"],
            capture_output=True, text=True, timeout=5,
        )
        symbols = {}
        for line in result.stdout.splitlines():
            line = line.strip().rstrip(";")
            if "=" in line:
                k, v = line.split("=", 1)
                symbols[k.strip()] = v.strip().strip('"')
        locale_info["number_format"] = {
            "decimal_separator": symbols.get("0", None),
            "grouping_separator": symbols.get("1", None),
        }
    except Exception:
        locale_info["number_format"] = None

    # Currency code (e.g. "USD")
    try:
        result = subprocess.run(
            ["defaults", "read", "NSGlobalDomain", "AppleLocale"],
            capture_output=True, text=True, timeout=5,
        )
        raw = result.stdout.strip()
        currency = None
        if "@currency=" in raw:
            currency = raw.split("@currency=")[1].split("@")[0]
        locale_info["currency_code"] = currency
    except Exception:
        locale_info["currency_code"] = None

    return locale_info


def collect_static_metrics() -> dict:
    """Gather hardware, OS, and display info (unchanging during a recording)."""
    static: dict = {}

    # Hostname
    try:
        static["hostname"] = socket.gethostname()
    except Exception:
        static["hostname"] = None

    # macOS version
    try:
        mac_ver = platform.mac_ver()[0]
        static["macos_version"] = mac_ver or None
    except Exception:
        static["macos_version"] = None

    # Kernel
    try:
        uname = platform.uname()
        static["kernel_version"] = f"{uname.system} {uname.release}"
    except Exception:
        static["kernel_version"] = None

    # CPU model
    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        static["cpu_model"] = result.stdout.strip() or None
    except Exception:
        static["cpu_model"] = None

    # CPU cores
    try:
        static["cpu_cores_physical"] = psutil.cpu_count(logical=False)
        static["cpu_cores_logical"] = psutil.cpu_count(logical=True)
    except Exception:
        static["cpu_cores_physical"] = None
        static["cpu_cores_logical"] = None

    # RAM
    try:
        mem = psutil.virtual_memory()
        static["memory_total_gb"] = round(mem.total / (1024**3), 1)
    except Exception:
        static["memory_total_gb"] = None

    # GPU model
    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        sp_data = json.loads(result.stdout)
        displays_data = sp_data.get("SPDisplaysDataType", [])
        if displays_data:
            static["gpu_model"] = displays_data[0].get("sppci_model", None)
        else:
            static["gpu_model"] = None
    except Exception:
        static["gpu_model"] = None

    # Python + screencap versions
    static["python_version"] = platform.python_version()
    static["screencap_version"] = __version__

    # Display info via mss
    try:
        with mss.mss() as sct:
            monitors = sct.monitors[1:]  # skip the "all monitors" entry
            displays = []
            for mon in monitors:
                displays.append(
                    {
                        "width": mon["width"],
                        "height": mon["height"],
                    }
                )
            static["displays"] = displays
            static["display_count"] = len(displays)
    except Exception:
        static["displays"] = []
        static["display_count"] = 0

    # Locale and language
    try:
        static["locale"] = _collect_locale()
    except Exception:
        static["locale"] = None

    return static


def collect_dynamic_metrics() -> dict:
    """Gather CPU, memory, disk, and battery snapshot."""
    dynamic: dict = {}

    dynamic["collected_at"] = datetime.now(timezone.utc).isoformat()

    # CPU usage
    try:
        dynamic["cpu_percent"] = psutil.cpu_percent(interval=0.5)
    except Exception:
        dynamic["cpu_percent"] = None

    # Memory
    try:
        mem = psutil.virtual_memory()
        dynamic["memory_used_gb"] = round(mem.used / (1024**3), 1)
        dynamic["memory_percent"] = mem.percent
    except Exception:
        dynamic["memory_used_gb"] = None
        dynamic["memory_percent"] = None

    # Disk
    try:
        disk = psutil.disk_usage("/")
        dynamic["disk_total_gb"] = round(disk.total / (1024**3), 1)
        dynamic["disk_available_gb"] = round(disk.free / (1024**3), 1)
    except Exception:
        dynamic["disk_total_gb"] = None
        dynamic["disk_available_gb"] = None

    # Battery
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            dynamic["battery_percent"] = round(battery.percent)
            dynamic["battery_charging"] = battery.power_plugged
        else:
            dynamic["battery_percent"] = None
            dynamic["battery_charging"] = None
    except Exception:
        dynamic["battery_percent"] = None
        dynamic["battery_charging"] = None

    return dynamic


def save_metrics(capture_dir: Path, phase: str) -> None:
    """Collect and write/update system_metrics.json in the recording directory.

    Args:
        capture_dir: Path to the recording directory.
        phase: "start" or "end".
    """
    metrics_path = capture_dir / METRICS_FILENAME

    if phase == "start":
        data = {
            "schema_version": SCHEMA_VERSION,
            "static": collect_static_metrics(),
            "start": collect_dynamic_metrics(),
            "end": None,
        }
        metrics_path.write_text(json.dumps(data, indent=2))

    elif phase == "end":
        if metrics_path.exists():
            try:
                data = json.loads(metrics_path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {"schema_version": SCHEMA_VERSION, "static": {}, "start": None}
        else:
            data = {"schema_version": SCHEMA_VERSION, "static": {}, "start": None}

        data["end"] = collect_dynamic_metrics()
        metrics_path.write_text(json.dumps(data, indent=2))
