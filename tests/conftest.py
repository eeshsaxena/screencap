"""Root conftest — shared fixtures for the screencap test suite.

All fixtures are function-scoped (unique tmp_path per test, no cross-test
contamination). DB fixtures use the real engine create_db + crud API so the
schema always matches production.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared cloud-auth provisioned-module test helpers
#
# ``_fake_provisioned`` / ``_install_provisioned`` let a test make
# ``from screencap import _provisioned`` resolve to a fabricated module (or
# force-absent), without writing a gitignored ``_provisioned.py`` to disk — so
# the suite passes whether or not an operator has generated one locally. Shared
# between tests/test_auth.py (credential-resolution precedence) and
# tests/test_cli.py (the ``_auth-config-check`` release guard); imported via
# ``from tests.conftest import _fake_provisioned, _install_provisioned``.
# ---------------------------------------------------------------------------


def _fake_provisioned(**attrs) -> types.ModuleType:
    mod = types.ModuleType("screencap._provisioned")
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install_provisioned(monkeypatch, mod: types.ModuleType | None) -> None:
    """Make ``from screencap import _provisioned`` resolve to ``mod``, or raise
    ImportError when ``mod is None`` — even if a gitignored ``_provisioned.py``
    exists on disk (an operator may have generated one to test cloud auth locally).

    Present case: setting the attribute on the package is what ``IMPORT_FROM`` checks
    first; sys.modules is kept in sync. Absent case: a ``None`` entry in
    ``sys.modules`` makes ``import screencap._provisioned`` raise ``ImportError``
    regardless of any on-disk module, and the package attribute is removed so the
    import machinery reaches that entry. monkeypatch reverts everything on teardown."""
    import screencap

    if mod is None:
        monkeypatch.delattr(screencap, "_provisioned", raising=False)
        # `None` in sys.modules → `import screencap._provisioned` raises ImportError
        # even when the file exists on disk: the robust "absent" state.
        monkeypatch.setitem(sys.modules, "screencap._provisioned", None)
    else:
        monkeypatch.setattr(screencap, "_provisioned", mod, raising=False)
        monkeypatch.setitem(sys.modules, "screencap._provisioned", mod)


# ---------------------------------------------------------------------------
# Phase 2 U1.6 removed both the legacy in-process ``screencap start`` path
# and the SessionController hand-off. The CLI is now a thin daemon HTTP
# client, and the ``SCREENCAP_LEGACY_START=1`` toggle has no effect. The
# legacy-style ``test_start_*`` tests that mock ``screencap.recorder.
# start_recording`` are kept but skipped — they exercise contracts now
# owned by the daemon supervisor (covered in tests/daemon/) and need
# rewriting as ``DaemonHTTPClient.start`` payload-assertion tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config cache reset (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture
def _signed_in(monkeypatch):
    """Default a test to "signed in" AND cloud-entitled so the cloud paths attach
    a bearer token and clear the U4 upload pre-check without touching the Keychain.

    Shared by the upload / download / CLI / upload-events suites (each opts in via
    a thin per-module autouse wrapper). It is deliberately NOT globally autouse:
    ``tests/test_auth.py`` exercises the real ``get_id_token`` refresh/rotation
    logic and must not have it stubbed out. Tests covering the not-signed-in / 401
    paths override this with their own ``monkeypatch.setattr`` (which wins).

    The entitlement pre-check (``auth.assert_entitled_to_upload``, U4) is stubbed
    to a no-op here so "signed in" implies "on an active plan" by default — the
    happy-path assumption every existing upload test was written under. Tests that
    exercise the pre-check itself override this stub (or test it in test_auth.py)."""
    monkeypatch.setattr(
        "screencap.auth.get_id_token", lambda force_refresh=False: "test-id-token"
    )
    monkeypatch.setattr("screencap.auth.assert_entitled_to_upload", lambda: None)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset screencap.config._config_cache between tests.

    Resets on BOTH setup and teardown. Tests that intentionally pre-set the
    cache should do so in the test body (after fixture setup), not at module
    level.
    """
    import screencap.config

    screencap.config._config_cache = None
    yield
    screencap.config._config_cache = None


# ---------------------------------------------------------------------------
# Real engine DB fixture
# ---------------------------------------------------------------------------


class RecordingDB:
    """Helper wrapping a real engine SQLite DB for test use.

    Provides the db_path, engine, session, and recording object plus
    convenience methods for inserting common event types.
    """

    def __init__(self, db_path, engine, session, recording):
        self.db_path = db_path
        self.engine = engine
        self.session = session
        self.recording = recording
        self._base_ts = recording.timestamp

    # -- convenience inserters ----------------------------------------------

    def add_click(self, ts_offset, *, x=500.0, y=300.0, button="left"):
        """Insert a click down+up pair at base_ts + ts_offset."""
        from screencap.engine.db import crud

        ts = self._base_ts + ts_offset
        crud.insert_action_event(self.session, self.recording, ts, {
            "name": "click",
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": True,
        })
        crud.insert_action_event(self.session, self.recording, ts + 0.05, {
            "name": "click",
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": False,
        })

    def add_keypress(self, ts_offset, char="h"):
        """Insert a key press+release pair at base_ts + ts_offset."""
        from screencap.engine.db import crud

        ts = self._base_ts + ts_offset
        crud.insert_action_event(self.session, self.recording, ts, {
            "name": "press",
            "key_char": char,
            "key_name": char,
            "canonical_key_char": char,
            "canonical_key_name": char,
        })
        crud.insert_action_event(self.session, self.recording, ts + 0.05, {
            "name": "release",
            "key_char": char,
            "key_name": char,
            "canonical_key_char": char,
            "canonical_key_name": char,
        })

    def add_window_event(
        self, ts_offset, *, title="Editor", bundle_id="com.app.editor",
        window_id="win-1", left=0, top=0, width=1920, height=1080,
        browser_url=None,
    ):
        """Insert a window event at base_ts + ts_offset."""
        from screencap.engine.db import crud

        ts = self._base_ts + ts_offset
        data = {
            "title": title,
            "app_bundle_id": bundle_id,
            "window_id": window_id,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }
        if browser_url is not None:
            data["browser_url"] = browser_url
        crud.insert_window_event(self.session, self.recording, ts, data)

    def close(self):
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def recording_db(tmp_path):
    """Create a real engine SQLite DB with configurable events.

    Uses screencap.engine.db.create_db + crud so the schema always matches
    the real engine. Returns a RecordingDB helper.
    """
    from screencap.engine.db import create_db, crud

    db_path = tmp_path / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })

    helper = RecordingDB(db_path, engine, session, recording)
    yield helper
    helper.close()


# ---------------------------------------------------------------------------
# FakeRecorder — medium-fidelity stand-in for screencap.engine.Recorder
# ---------------------------------------------------------------------------


class FakeRecorder:
    """Stand-in for screencap.engine.Recorder (external hardware boundary).

    Creates a real recording.db on __enter__ using the engine's create_db +
    crud API so downstream code (export, catalog) works with real data.
    Sets is_recording=False so the live-display loop exits immediately.
    """

    def __init__(self, capture_dir_str, **kwargs):
        self.capture_dir = str(Path(capture_dir_str).resolve())
        self.is_recording = False
        self.health_warning = ""
        self.child_crashes = []
        self.recording_duration = None
        self._stopped = False
        self._chunk_process_q = None
        self._audio_ack_q = None
        self._flush_requested = None
        self._flush_ack_counter = None

    def __enter__(self):
        self._create_db()
        return self

    def __exit__(self, *args):
        return False

    def wait_for_ready(self, timeout=30):
        return True

    def stop(self):
        self.is_recording = False
        self._stopped = True

    def finalize_pipeline(self):
        """Stand-in for the engine's pre-__exit__ thread drain. No-op for fakes."""
        return None

    def _create_db(self):
        """Create a recording.db with minimal realistic data."""
        from screencap.engine.db import create_db, crud

        db_path = Path(self.capture_dir) / "recording.db"
        engine, Session = create_db(str(db_path))
        session = Session()

        t = 1000.0
        recording = crud.insert_recording(session, {
            "timestamp": t,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        })

        # Window event
        crud.insert_window_event(session, recording, t + 0.1, {
            "title": "Documents",
            "app_bundle_id": "com.apple.finder",
            "window_id": "win-1",
            "left": 0, "top": 0, "width": 1920, "height": 1080,
        })

        # Click pair
        crud.insert_action_event(session, recording, t + 0.5, {
            "name": "click",
            "mouse_x": 500.0, "mouse_y": 300.0,
            "mouse_button_name": "left", "mouse_pressed": True,
            "window_event_timestamp": t + 0.1,
        })
        crud.insert_action_event(session, recording, t + 0.55, {
            "name": "click",
            "mouse_x": 500.0, "mouse_y": 300.0,
            "mouse_button_name": "left", "mouse_pressed": False,
            "window_event_timestamp": t + 0.1,
        })

        # Key press+release
        crud.insert_action_event(session, recording, t + 1.0, {
            "name": "press",
            "key_char": "h", "key_name": "h",
            "canonical_key_char": "h", "canonical_key_name": "h",
            "window_event_timestamp": t + 0.1,
        })
        crud.insert_action_event(session, recording, t + 1.05, {
            "name": "release",
            "key_char": "h", "key_name": "h",
            "canonical_key_char": "h", "canonical_key_name": "h",
            "window_event_timestamp": t + 0.1,
        })

        session.close()
        engine.dispose()
