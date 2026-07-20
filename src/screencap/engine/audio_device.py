"""Bluetooth-aware input-device classification + mic-source selection (SCR-288).

Screencap holds the mic ``InputStream`` open for the whole recording. On a
Bluetooth device (AirPods) that forces the link from A2DP (rich stereo output)
into HFP/SCO ("phone call" quality), degrading the audio the *user hears*, not
just the mic input. We route around it: when the input would be a Bluetooth
device, capture from the built-in mic instead so playback stays A2DP. See
``docs/plans/2026-07-20-003-fix-bluetooth-mic-redirect-plan.md``.

Two layers, deliberately separated so the policy is CI-testable while the
hardware reads stay mockable:

* :func:`classify_input_devices` — reads each device's CoreAudio transport type
  and ``IsRunningSomewhere`` via ctypes, name-keyed to the ``sounddevice`` /
  PortAudio index that :func:`sounddevice.InputStream` can actually open. Every
  read is fail-open: an unreadable device is ``"unknown"`` and a failure to load
  CoreAudio at all yields an empty list, so the caller falls back to today's
  default-device behaviour rather than crashing.
* :func:`select_mic_source` — a pure function over the classified list plus the
  ``prefer_builtin`` toggle. No I/O, no ctypes; fully unit-testable with
  fabricated device lists.

Transport classification uses ``kAudioDevicePropertyTransportType`` — the
canonical, locale-proof signal — never device-name substrings. The CoreAudio →
PortAudio join, however, *is* name-keyed: ``sounddevice.query_devices()`` exposes
only ``name`` (no CoreAudio UID/AudioObjectID), and the CoreAudio device name
matches PortAudio's name verbatim (verified on-device). A device that cannot be
matched to a PortAudio index is dropped, so the caller fails open to the default.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from loguru import logger

# --- Transport classes -----------------------------------------------------

TRANSPORT_BUILTIN = "builtin"
TRANSPORT_BLUETOOTH = "bluetooth"
TRANSPORT_BLUETOOTH_LE = "bluetoothLE"
TRANSPORT_USB = "usb"
TRANSPORT_UNKNOWN = "unknown"

# Real, non-Bluetooth microphones we are willing to redirect *to*. A virtual or
# aggregate device (e.g. ZoomAudioDevice) is not a physical mic — redirecting to
# it would capture loopback/nothing — so those are never redirect targets.
_REDIRECT_TARGET_TRANSPORTS = (TRANSPORT_BUILTIN, TRANSPORT_USB)

_BLUETOOTH_TRANSPORTS = (TRANSPORT_BLUETOOTH, TRANSPORT_BLUETOOTH_LE)


# --- Selection result ------------------------------------------------------

# Selection actions.
ACTION_DEVICE = "device"  # open this specific PortAudio index
ACTION_DEFAULT = "default"  # open the OS default input (device=None) — unchanged
ACTION_SKIP = "skip"  # do not capture (protect Bluetooth playback)

# Log reasons (R13 + the `default` extension for the unchanged/fail-open path).
REASON_REDIRECT = "redirect"
REASON_FALLBACK_CAPTURE = "fallback-capture"
REASON_FALLBACK_SKIP = "fallback-skip"
REASON_DEFAULT = "default"


@dataclass(frozen=True)
class InputDevice:
    """One input-capable device, joined across CoreAudio and PortAudio."""

    index: int  # PortAudio/sounddevice device index (openable)
    name: str
    transport: str  # one of the TRANSPORT_* constants
    input_channels: int
    running_elsewhere: bool  # another process already has this device running


@dataclass(frozen=True)
class Selection:
    """Where the audio capture stream should be opened (or not)."""

    action: str  # ACTION_DEVICE | ACTION_DEFAULT | ACTION_SKIP
    index: int | None  # PortAudio index when action == ACTION_DEVICE
    reason: str  # one of the REASON_* constants (for logging)


def is_bluetooth(transport: str) -> bool:
    return transport in _BLUETOOTH_TRANSPORTS


# --- CoreAudio classification (hardware; fail-open) -------------------------

# ``kAudioDevicePropertyTransportType`` values, as four-char codes.
_CA_TRANSPORTS = {
    "bltn": TRANSPORT_BUILTIN,
    "blue": TRANSPORT_BLUETOOTH,
    "blea": TRANSPORT_BLUETOOTH_LE,
    "usb ": TRANSPORT_USB,
}


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


def classify_input_devices() -> list[InputDevice]:
    """Return input-capable devices with transport + in-use classification.

    Fail-open by contract: if CoreAudio cannot be loaded or the top-level device
    enumeration fails, return ``[]`` so the caller uses the OS default input
    (today's behaviour). Per-device read failures degrade that device to
    ``transport="unknown"`` rather than dropping it.

    Only devices that also resolve to a ``sounddevice`` (PortAudio) index — the
    identifier :func:`sounddevice.InputStream` needs — are returned; the join is
    by device name.
    """
    try:
        pa_inputs = _portaudio_inputs()
        if not pa_inputs:
            return []
        ca_by_name = _coreaudio_inputs_by_name()
    except Exception as exc:  # noqa: BLE001 — classification must never crash capture.
        logger.warning(f"Input-device classification failed (fail-open): {exc}")
        return []

    devices: list[InputDevice] = []
    for index, name, channels in pa_inputs:
        transport, running = ca_by_name.get(name, (TRANSPORT_UNKNOWN, False))
        devices.append(
            InputDevice(
                index=index,
                name=name,
                transport=transport,
                input_channels=channels,
                running_elsewhere=running,
            )
        )
    return devices


def _portaudio_inputs() -> list[tuple[int, str, int]]:
    """``(index, name, input_channels)`` for every openable input device."""
    import sounddevice

    out: list[tuple[int, str, int]] = []
    for idx, dev in enumerate(sounddevice.query_devices()):
        channels = int(dev.get("max_input_channels", 0) or 0)
        if channels > 0:
            out.append((idx, str(dev["name"]), channels))
    return out


def _coreaudio_inputs_by_name() -> dict[str, tuple[str, bool]]:
    """Map input-device name -> ``(transport, running_elsewhere)`` via CoreAudio.

    A thin ctypes wrapper over ``AudioObjectGetPropertyData``. Isolated here so
    the rest of the module (and its tests) never touch ctypes; monkeypatch this
    function to exercise :func:`classify_input_devices` without hardware.
    """
    import ctypes
    import ctypes.util

    ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]

    sys_obj = 1  # kAudioObjectSystemObject
    sel_devices = _fourcc("dev#")  # kAudioHardwarePropertyDevices
    sel_name = _fourcc("lnam")  # kAudioObjectPropertyName
    sel_transport = _fourcc("tran")  # kAudioDevicePropertyTransportType
    sel_streams = _fourcc("slay")  # kAudioDevicePropertyStreamConfiguration
    sel_running = _fourcc("gone")  # kAudioDevicePropertyDeviceIsRunningSomewhere
    scope_global = _fourcc("glob")  # kAudioObjectPropertyScopeGlobal
    scope_input = _fourcc("inpt")  # kAudioObjectPropertyScopeInput
    elem_main = 0
    utf8 = 0x08000100  # kCFStringEncodingUTF8

    class Addr(ctypes.Structure):
        _fields_ = [
            ("mSelector", ctypes.c_uint32),
            ("mScope", ctypes.c_uint32),
            ("mElement", ctypes.c_uint32),
        ]

    def _read(obj, sel, buf, scope=scope_global):
        addr = Addr(sel, scope, elem_main)
        size = ctypes.c_uint32(ctypes.sizeof(buf))
        status = ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(buf)
        )
        return status == 0

    def _read_size(obj, sel, scope=scope_global):
        addr = Addr(sel, scope, elem_main)
        size = ctypes.c_uint32(0)
        status = ca.AudioObjectGetPropertyDataSize(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size)
        )
        return size.value if status == 0 else 0

    def _name(dev):
        cfstr = ctypes.c_void_p()
        if not _read(dev, sel_name, cfstr) or not cfstr.value:
            return None
        try:
            buf = ctypes.create_string_buffer(512)
            if not cf.CFStringGetCString(cfstr, buf, len(buf), utf8):
                return None
            return buf.value.decode("utf-8", "replace")
        finally:
            cf.CFRelease(cfstr)

    def _input_channels(dev):
        size = _read_size(dev, sel_streams, scope_input)
        if size < 4:
            return 0
        buf = (ctypes.c_byte * size)()
        addr = Addr(sel_streams, scope_input, elem_main)
        szc = ctypes.c_uint32(size)
        if ca.AudioObjectGetPropertyData(
            dev, ctypes.byref(addr), 0, None, ctypes.byref(szc), buf
        ) != 0:
            return 0
        raw = bytes(buf)
        nbuf = struct.unpack("I", raw[:4])[0]
        # AudioBufferList: UInt32 mNumberBuffers, then AudioBuffer[]
        # {UInt32 mNumberChannels; UInt32 mDataByteSize; void* mData} (16 bytes each).
        channels, off = 0, 8
        for _ in range(nbuf):
            if off + 4 > len(raw):
                break
            channels += struct.unpack("I", raw[off:off + 4])[0]
            off += 16
        return channels

    # Enumerate all devices.
    size = _read_size(sys_obj, sel_devices)
    count = size // ctypes.sizeof(ctypes.c_uint32)
    if count <= 0:
        return {}
    dev_ids = (ctypes.c_uint32 * count)()
    if not _read(sys_obj, sel_devices, dev_ids):
        return {}

    result: dict[str, tuple[str, bool]] = {}
    for dev in dev_ids:
        try:
            if _input_channels(dev) <= 0:
                continue
            name = _name(dev)
            if not name:
                continue
            transport = ctypes.c_uint32(0)
            if _read(dev, sel_transport, transport):
                code = struct.pack(">I", transport.value).decode("ascii", "replace")
                transport_cls = _CA_TRANSPORTS.get(code, TRANSPORT_UNKNOWN)
            else:
                transport_cls = TRANSPORT_UNKNOWN
            running = ctypes.c_uint32(0)
            running_elsewhere = bool(running.value) if _read(dev, sel_running, running) else False
            result[name] = (transport_cls, running_elsewhere)
        except Exception:  # noqa: BLE001 — a bad device must not drop the whole list.
            continue
    return result


# --- Selection policy (pure) -----------------------------------------------

def select_mic_source(
    devices: list[InputDevice], default_index: int | None, prefer_builtin: bool
) -> Selection:
    """Decide where to capture the mic, given classified devices and the toggle.

    Pure and side-effect-free (no ctypes, no I/O) so it is fully CI-testable.

    * Redirect off, or the default input is unknown/absent/not Bluetooth →
      capture from the OS default (unchanged behaviour, fail-open).
    * Default is Bluetooth and a non-Bluetooth mic (built-in/USB) exists →
      capture from that mic (``redirect``): AirPods stay in A2DP.
    * Default is Bluetooth with no non-Bluetooth alternative → capture from the
      Bluetooth mic only if it is already running elsewhere (a call already
      forced SCO, so we add no harm — ``fallback-capture``); otherwise skip to
      protect playback (``fallback-skip``).
    """
    if not prefer_builtin:
        return Selection(ACTION_DEFAULT, None, REASON_DEFAULT)

    default_dev = _find(devices, default_index)
    if default_dev is None or default_dev.transport == TRANSPORT_UNKNOWN:
        return Selection(ACTION_DEFAULT, None, REASON_DEFAULT)
    if not is_bluetooth(default_dev.transport):
        return Selection(ACTION_DEFAULT, None, REASON_DEFAULT)

    alternative = _find_redirect_target(devices)
    if alternative is not None:
        return Selection(ACTION_DEVICE, alternative.index, REASON_REDIRECT)

    if default_dev.running_elsewhere:
        return Selection(ACTION_DEVICE, default_dev.index, REASON_FALLBACK_CAPTURE)
    return Selection(ACTION_SKIP, None, REASON_FALLBACK_SKIP)


def resolve_open_target(
    devices: list[InputDevice], default_index: int | None, prefer_builtin: bool
) -> tuple[int | None, bool, str]:
    """Combine :func:`select_mic_source` with concrete-default-index pinning.

    Returns ``(device_index_or_None, skip, reason)`` for the stream factory. When
    the policy says "use the OS default", this pins to the default's *concrete*
    index if it is visible in the classified list — so an already-open stream
    cannot drift onto a newly-connected AirPods (KTD-3) — falling back to the true
    default (``None``) only when the index is unknown (e.g. fail-open).
    """
    sel = select_mic_source(devices, default_index, prefer_builtin)
    if sel.action == ACTION_SKIP:
        return (None, True, sel.reason)
    if sel.action == ACTION_DEVICE:
        return (sel.index, False, sel.reason)
    known = default_index is not None and any(d.index == default_index for d in devices)
    return (default_index if known else None, False, sel.reason)


def _find(devices: list[InputDevice], index: int | None) -> InputDevice | None:
    if index is None:
        return None
    return next((d for d in devices if d.index == index), None)


def _find_redirect_target(devices: list[InputDevice]) -> InputDevice | None:
    """A real, non-Bluetooth mic to redirect to — built-in preferred, then USB."""
    for transport in _REDIRECT_TARGET_TRANSPORTS:
        match = next(
            (d for d in devices if d.transport == transport and d.input_channels > 0),
            None,
        )
        if match is not None:
            return match
    return None
