"""Screenshot retention bound (search guardrails U5 / R2).

Age + size-cap eviction of the ``screenshots/`` dir, the don't-race-the-indexer
guard, content-index row purge, and the periodic daemon sweep. Vision-free and
Keychain-free (plaintext content index; the index path is redirected to tmp).
"""

from __future__ import annotations

import asyncio

import pytest

from screencap import content_index, retention
from screencap.content_index import ContentIndex, IndexFrame
from screencap.daemon.retention_sweep import RetentionSweep, sweep_once

pytestmark = pytest.mark.privacy

_NOW = 1_700_000_000.0
_DAY = 86400.0


def _make_still(screenshots_dir, ts: float, size: int = 1000, enc: bool = False):
    name = f"{ts:.6f}.jpg" + (".enc" if enc else "")
    p = screenshots_dir / name
    p.write_bytes(b"x" * size)
    return p


@pytest.fixture
def rec_dir(tmp_path):
    d = tmp_path / "recordings" / "rec-1"
    (d / "screenshots").mkdir(parents=True)
    return d


@pytest.fixture
def redirect_index(tmp_path, monkeypatch):
    """Point the global content-index path at tmp so the purge never touches ~."""
    idx_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: idx_path)
    return idx_path


# ---------------------------------------------------------------------------
# Age policy
# ---------------------------------------------------------------------------


def test_age_policy_evicts_old_and_purges_index(rec_dir, redirect_index):
    ss = rec_dir / "screenshots"
    old_ts = _NOW - 40 * _DAY
    new_ts = _NOW - 1 * _DAY
    _make_still(ss, old_ts)
    _make_still(ss, new_ts)

    with ContentIndex(redirect_index) as ix:
        ix.write_frames(
            "rec-1",
            [
                IndexFrame(timestamp_ms=round(old_ts * 1000), text="oldsentineltext"),
                IndexFrame(timestamp_ms=round(new_ts * 1000), text="newsentineltext"),
            ],
        )

    report = retention.evict_screenshots(
        rec_dir, now=_NOW, days=30, size_cap_mb=0, last_indexed_ts=None
    )

    assert len(report.evicted) == 1
    assert not (ss / f"{old_ts:.6f}.jpg").exists()
    assert (ss / f"{new_ts:.6f}.jpg").exists()
    assert report.index_rows_purged == 1

    with ContentIndex(redirect_index) as ix:
        assert ix.search("oldsentineltext").hits == []  # purged
        assert ix.search("newsentineltext").hits  # survived


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


def test_size_cap_evicts_oldest_first(rec_dir, redirect_index):
    ss = rec_dir / "screenshots"
    # 5 stills of ~1 MiB each; cap 3 MiB → evict the 2 oldest to land at 3 MiB.
    tss = [_NOW - i * 3600 for i in range(5)]  # i=0 newest … i=4 oldest
    for ts in tss:
        _make_still(ss, ts, size=1024 * 1024)

    report = retention.evict_screenshots(
        rec_dir, now=_NOW, days=0, size_cap_mb=3, last_indexed_ts=None
    )

    assert len(report.evicted) == 2
    remaining = sorted(float(p.name[:-4]) for p in ss.glob("*.jpg"))
    assert len(remaining) == 3
    # The oldest two (largest age) are the ones gone.
    assert min(remaining) == tss[2]


# ---------------------------------------------------------------------------
# Don't race the indexer
# ---------------------------------------------------------------------------


def test_stills_newer_than_last_indexed_survive(rec_dir, redirect_index):
    ss = rec_dir / "screenshots"
    old_ts = _NOW - 40 * _DAY
    newer_ts = _NOW - 35 * _DAY  # still older than the 30d cutoff…
    _make_still(ss, old_ts)
    _make_still(ss, newer_ts)

    # …but the indexer only reached old_ts, so newer_ts is off-limits this pass.
    report = retention.evict_screenshots(
        rec_dir, now=_NOW, days=30, size_cap_mb=0, last_indexed_ts=old_ts
    )

    assert len(report.evicted) == 1
    assert not (ss / f"{old_ts:.6f}.jpg").exists()
    assert (ss / f"{newer_ts:.6f}.jpg").exists()  # deferred — indexer hasn't reached it


def test_encrypted_stills_are_evicted_too(rec_dir, redirect_index):
    ss = rec_dir / "screenshots"
    old_ts = _NOW - 40 * _DAY
    _make_still(ss, old_ts, enc=True)  # <ts>.jpg.enc

    report = retention.evict_screenshots(
        rec_dir, now=_NOW, days=30, size_cap_mb=0, last_indexed_ts=None
    )
    assert len(report.evicted) == 1
    assert not (ss / f"{old_ts:.6f}.jpg.enc").exists()


# ---------------------------------------------------------------------------
# Unbounded = today's behavior
# ---------------------------------------------------------------------------


def test_unbounded_config_evicts_nothing(rec_dir, redirect_index):
    ss = rec_dir / "screenshots"
    _make_still(ss, _NOW - 100 * _DAY)  # very old, but no bound set

    report = retention.evict_screenshots(
        rec_dir, now=_NOW, days=0, size_cap_mb=0, last_indexed_ts=None
    )
    assert report.evicted == []
    assert len(list(ss.glob("*.jpg"))) == 1


# ---------------------------------------------------------------------------
# Periodic sweep (converged recordings, signed-out)
# ---------------------------------------------------------------------------


def test_sweep_evicts_converged_recording(tmp_path, redirect_index, monkeypatch):
    from screencap import config

    monkeypatch.setenv("SCREENCAP_SCREENSHOT_RETENTION_DAYS", "30")
    monkeypatch.setenv("SCREENCAP_SCREENSHOT_SIZE_CAP_MB", "0")
    config.invalidate_config_cache()

    recordings = tmp_path / "recordings"
    ss = recordings / "rec-old" / "screenshots"
    ss.mkdir(parents=True)
    _make_still(ss, _NOW - 40 * _DAY)
    _make_still(ss, _NOW - 1 * _DAY)

    # No auth setup at all → the sweep runs signed-out.
    report = sweep_once(recordings, now=_NOW)

    assert report.recordings_scanned == 1
    assert report.stills_evicted == 1
    assert len(list(ss.glob("*.jpg"))) == 1


def test_sweep_noop_when_unbounded(tmp_path, monkeypatch):
    from screencap import config

    monkeypatch.setenv("SCREENCAP_SCREENSHOT_RETENTION_DAYS", "0")
    monkeypatch.setenv("SCREENCAP_SCREENSHOT_SIZE_CAP_MB", "0")
    config.invalidate_config_cache()

    recordings = tmp_path / "recordings"
    ss = recordings / "rec" / "screenshots"
    ss.mkdir(parents=True)
    _make_still(ss, _NOW - 100 * _DAY)

    report = sweep_once(recordings, now=_NOW)
    assert report.recordings_scanned == 0
    assert report.stills_evicted == 0
    assert len(list(ss.glob("*.jpg"))) == 1


def test_retention_sweep_start_and_shutdown_clean():
    async def run():
        sw = RetentionSweep(interval_secs=0.01)
        sw.start()
        await asyncio.sleep(0.03)  # let a tick or two fire (unbounded → no-op)
        await sw.shutdown(timeout=1.0)

    asyncio.run(run())
