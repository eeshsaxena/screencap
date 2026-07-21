"""Mic capture rate + software resampling (shared-mic buffer-collapse fix).

``record_audio`` used to open the default input device directly at the 16 kHz
pipeline target. On macOS that non-native request makes CoreAudio collapse the
device's GLOBAL buffer-frame-size to its minimum, which is shared by every
client of that mic — so a concurrent meeting app (Zoom/Meet/Teams) gets driven
at a sub-millisecond IOProc cadence and the user's outbound voice glitches until
the recording stops.

The fix captures at the device's NATIVE rate and resamples to 16 kHz in
software. The hardware-coupled parts (the real ``sounddevice`` stream, the FLAC
writer) need a mic, but the two correctness-critical seams are unit-testable:

* ``resolve_capture_rate`` — the requested capture rate MUST track the device's
  native ``default_samplerate``, never the hard-coded 16 kHz (the assertion that
  would have caught the bug), with a safe fallback when the device can't be
  queried.
* ``resample_capture_block`` — one long-lived resampler converts native-rate
  float32 mono blocks to the 16 kHz on-disk rate, gaplessly.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import av
import numpy as np

from screencap.engine.recorder import resample_capture_block, resolve_capture_rate


def _fake_sounddevice(*, default_samplerate=None, raise_on_query=False, by_index=None):
    """A stand-in ``sounddevice`` module so the test needs no real mic/device.

    ``by_index`` maps a device index -> its native rate, exercising the
    device-scoped ``resolve_capture_rate(device=...)`` path; without it, the
    default-input path (``kind="input"``) returns ``default_samplerate``.
    """
    mod = types.ModuleType("sounddevice")

    def query_devices(device=None, kind=None):
        if raise_on_query:
            raise RuntimeError("no input device")
        if device is not None and by_index is not None:
            return {"name": f"Dev{device}", "default_samplerate": by_index[device]}
        return {"name": "Fake Mic", "default_samplerate": default_samplerate}

    mod.query_devices = query_devices
    return mod


def test_resolve_capture_rate_uses_device_native_rate():
    """Regression: capture rate follows the device (48000), NOT the 16 kHz target.

    Opening the shared mic at 16 kHz is what collapsed the device buffer; this
    asserts we request the native rate instead.
    """
    fake = _fake_sounddevice(default_samplerate=48000.0)
    with mock.patch.dict(sys.modules, {"sounddevice": fake}):
        assert resolve_capture_rate(16000) == 48000


def test_resolve_capture_rate_handles_44100_device():
    fake = _fake_sounddevice(default_samplerate=44100.0)
    with mock.patch.dict(sys.modules, {"sounddevice": fake}):
        assert resolve_capture_rate(16000) == 44100


def test_resolve_capture_rate_falls_back_on_query_error():
    """A device-query failure must degrade to the target rate, not crash."""
    fake = _fake_sounddevice(raise_on_query=True)
    with mock.patch.dict(sys.modules, {"sounddevice": fake}):
        assert resolve_capture_rate(16000) == 16000


def test_resolve_capture_rate_uses_selected_device():
    """SCR-288 R8: the rate tracks the *selected* device, not the OS default.

    A muted-start recording later unmuted onto the built-in mic must resolve for
    that built-in device (48000), not the default input (Bluetooth) sampled at
    process start.
    """
    fake = _fake_sounddevice(default_samplerate=16000.0, by_index={7: 48000.0})
    with mock.patch.dict(sys.modules, {"sounddevice": fake}):
        assert resolve_capture_rate(16000, device=7) == 48000


def test_resolve_capture_rate_device_query_error_falls_back():
    """A failed query for a specific device still degrades to target_rate."""
    fake = _fake_sounddevice(raise_on_query=True)
    with mock.patch.dict(sys.modules, {"sounddevice": fake}):
        assert resolve_capture_rate(16000, device=3) == 16000


def test_resample_capture_block_downsamples_native_to_16k():
    """A native-rate block resamples to ~1/3 the samples at 16 kHz, float32 (M,1)."""
    src_rate, dst_rate = 48000, 16000
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=dst_rate)

    # 1 second of native-rate mono audio, shaped like a drained capture buffer.
    t = np.linspace(0, 1, src_rate, endpoint=False, dtype=np.float32)
    block = (0.2 * np.sin(2 * np.pi * 220 * t)).reshape(-1, 1)

    out = resample_capture_block(resampler, block, src_rate)
    tail = resample_capture_block(resampler, None, src_rate)  # flush residual

    assert out is not None
    assert out.dtype == np.float32
    assert out.shape[1] == 1  # mono, FLAC-writer shape

    total = out.shape[0] + (0 if tail is None else tail.shape[0])
    # ~16000 out for 48000 in (1/3), within the resampler's small filter delay.
    assert dst_rate - 200 <= total <= dst_rate + 200
    # And it genuinely downsampled — far fewer samples than the 48000 input.
    assert total < src_rate // 2


def test_resample_capture_block_stays_gapless_across_blocks():
    """Two consecutive native blocks resample to ~the same total as one big block.

    The shared resampler must not drop/duplicate samples at block boundaries
    (per-block independent resampling would click/gap the audio).
    """
    src_rate, dst_rate = 48000, 16000
    half = src_rate // 2

    t = np.linspace(0, 1, src_rate, endpoint=False, dtype=np.float32)
    signal = (0.2 * np.sin(2 * np.pi * 220 * t)).reshape(-1, 1)

    r = av.AudioResampler(format="fltp", layout="mono", rate=dst_rate)
    a = resample_capture_block(r, signal[:half], src_rate)
    b = resample_capture_block(r, signal[half:], src_rate)
    tail = resample_capture_block(r, None, src_rate)
    streamed = sum(x.shape[0] for x in (a, b, tail) if x is not None)

    assert dst_rate - 200 <= streamed <= dst_rate + 200
