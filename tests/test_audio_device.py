"""Bluetooth-aware mic-source selection + input-device classification (SCR-288).

The hardware-coupled half (the real CoreAudio ctypes reads) needs a Mac with the
right devices attached and is validated manually; the two CI-testable seams are:

* ``select_mic_source`` — the pure decision (redirect to built-in on Bluetooth,
  capture-if-in-call-else-skip when only Bluetooth exists, fail-open otherwise).
  Every Acceptance Example maps to a case here.
* ``classify_input_devices`` — the CoreAudio → PortAudio join, exercised with the
  two hardware calls monkeypatched, including the fail-open contract.
"""

from __future__ import annotations

from screencap.engine import audio_device as ad
from screencap.engine.audio_device import (
    ACTION_DEFAULT,
    ACTION_DEVICE,
    ACTION_SKIP,
    REASON_DEFAULT,
    REASON_FALLBACK_CAPTURE,
    REASON_FALLBACK_SKIP,
    REASON_REDIRECT,
    InputDevice,
    select_mic_source,
)


def _dev(index, transport, *, name=None, channels=1, running=False):
    return InputDevice(
        index=index,
        name=name or f"{transport}-{index}",
        transport=transport,
        input_channels=channels,
        running_elsewhere=running,
    )


# --- select_mic_source: the pure policy ------------------------------------

def test_non_bluetooth_default_uses_default():
    """AE5 / R2: no Bluetooth involved → unchanged default-device behaviour."""
    devices = [_dev(1, "builtin")]
    sel = select_mic_source(devices, default_index=1, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_DEFAULT, None, REASON_DEFAULT)


def test_bluetooth_default_redirects_to_builtin():
    """AE1 / R1: Bluetooth default + built-in available → capture from built-in."""
    devices = [_dev(2, "bluetooth"), _dev(1, "builtin")]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_DEVICE, 1, REASON_REDIRECT)


def test_builtin_wins_even_when_bluetooth_in_use():
    """AE2: on a call (Bluetooth running elsewhere) the built-in still wins."""
    devices = [_dev(2, "bluetooth", running=True), _dev(1, "builtin")]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel.action == ACTION_DEVICE and sel.index == 1
    assert sel.reason == REASON_REDIRECT


def test_bluetooth_only_in_call_captures_bluetooth():
    """AE4 / R4: only Bluetooth input, already running elsewhere → capture it."""
    devices = [_dev(2, "bluetooth", running=True)]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_DEVICE, 2, REASON_FALLBACK_CAPTURE)


def test_bluetooth_only_not_in_call_skips():
    """AE3 / R5: only Bluetooth input, nobody else using it → skip (protect playback)."""
    devices = [_dev(2, "bluetooth", running=False)]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_SKIP, None, REASON_FALLBACK_SKIP)


def test_toggle_off_never_redirects():
    """R11: prefer_builtin False keeps the Bluetooth (default) device."""
    devices = [_dev(2, "bluetooth"), _dev(1, "builtin")]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=False)
    assert sel == ad.Selection(ACTION_DEFAULT, None, REASON_DEFAULT)


def test_unknown_default_fails_open():
    """KTD-2: an unclassifiable default device → default behaviour, never skip."""
    devices = [_dev(2, "unknown")]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_DEFAULT, None, REASON_DEFAULT)


def test_missing_default_index_fails_open():
    """A default index absent from the classified list → default (fail-open)."""
    devices = [_dev(1, "builtin")]
    sel = select_mic_source(devices, default_index=None, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_DEFAULT, None, REASON_DEFAULT)


def test_usb_mic_is_a_valid_redirect_target():
    """A USB mic (non-Bluetooth) is a legitimate redirect target when no built-in."""
    devices = [_dev(2, "bluetooth"), _dev(5, "usb")]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_DEVICE, 5, REASON_REDIRECT)


def test_virtual_device_is_not_a_redirect_target():
    """A virtual device (e.g. ZoomAudioDevice) must not be redirected to; skip instead."""
    devices = [_dev(2, "bluetooth"), _dev(3, "unknown", name="ZoomAudioDevice")]
    sel = select_mic_source(devices, default_index=2, prefer_builtin=True)
    assert sel == ad.Selection(ACTION_SKIP, None, REASON_FALLBACK_SKIP)


# --- resolve_open_target: selection + concrete-index pinning ----------------

def test_resolve_open_target_redirect_returns_device_index():
    devices = [_dev(2, "bluetooth"), _dev(1, "builtin")]
    assert ad.resolve_open_target(devices, 2, True) == (1, False, REASON_REDIRECT)


def test_resolve_open_target_skip():
    devices = [_dev(2, "bluetooth")]
    assert ad.resolve_open_target(devices, 2, True) == (None, True, REASON_FALLBACK_SKIP)


def test_resolve_open_target_pins_concrete_default_index():
    """R2/KTD-3: the non-Bluetooth default is opened by its concrete index, not None,
    so an already-open stream can't drift onto a newly-connected AirPods."""
    devices = [_dev(1, "builtin")]
    assert ad.resolve_open_target(devices, 1, True) == (1, False, REASON_DEFAULT)


def test_resolve_open_target_falls_back_to_true_default_when_index_unknown():
    """Fail-open (empty classification / unknown default) → device=None (true default)."""
    assert ad.resolve_open_target([], 1, True) == (None, False, REASON_DEFAULT)


# --- classify_input_devices: the CoreAudio -> PortAudio join ----------------

def test_classify_joins_portaudio_and_coreaudio(monkeypatch):
    monkeypatch.setattr(
        ad, "_portaudio_inputs",
        lambda: [(1, "MacBook Pro Microphone", 1), (3, "AirPods Pro", 1)],
    )
    monkeypatch.setattr(
        ad, "_coreaudio_inputs_by_name",
        lambda: {
            "MacBook Pro Microphone": ("builtin", False),
            "AirPods Pro": ("bluetooth", True),
        },
    )
    devices = ad.classify_input_devices()
    by_name = {d.name: d for d in devices}
    assert by_name["MacBook Pro Microphone"].transport == "builtin"
    assert by_name["AirPods Pro"].transport == "bluetooth"
    assert by_name["AirPods Pro"].running_elsewhere is True
    assert by_name["AirPods Pro"].index == 3


def test_classify_unmatched_portaudio_device_is_unknown(monkeypatch):
    """A PortAudio device CoreAudio didn't classify → transport unknown, not dropped."""
    monkeypatch.setattr(ad, "_portaudio_inputs", lambda: [(1, "Ghost Mic", 2)])
    monkeypatch.setattr(ad, "_coreaudio_inputs_by_name", lambda: {})
    devices = ad.classify_input_devices()
    assert len(devices) == 1
    assert devices[0].transport == "unknown"
    assert devices[0].running_elsewhere is False


def test_classify_empty_when_no_portaudio_inputs(monkeypatch):
    monkeypatch.setattr(ad, "_portaudio_inputs", lambda: [])
    assert ad.classify_input_devices() == []


def test_classify_fails_open_on_coreaudio_error(monkeypatch):
    """A CoreAudio read failure yields [] so the caller uses the OS default."""
    monkeypatch.setattr(ad, "_portaudio_inputs", lambda: [(1, "Mic", 1)])

    def _boom():
        raise RuntimeError("CoreAudio unavailable")

    monkeypatch.setattr(ad, "_coreaudio_inputs_by_name", _boom)
    assert ad.classify_input_devices() == []
