"""Collect system metrics for each recording."""

from __future__ import annotations

import json
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import mss
import psutil

from screencap import __version__

METRICS_FILENAME = "system_metrics.json"
SCHEMA_VERSION = 1


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
