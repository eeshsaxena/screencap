"""U3 — natural-language time-window resolver (dependency-free).

Deterministic: every case passes a fixed ``now_ms`` and ``tz=UTC`` so the windows
don't depend on the host clock or timezone. Covers the supported phrase set, the
KTD3 clamp rule (future/empty windows), and the unrecognized-phrase fall-through.
"""

from __future__ import annotations

import datetime as dt
from datetime import timezone

import pytest

from screencap.recall.timeparse import resolve_time_window

UTC = timezone.utc
pytestmark = pytest.mark.privacy


def _ms(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp() * 1000)


# Friday 2026-07-10 14:30:00 UTC.
NOW = _ms(2026, 7, 10, 14, 30)


def test_yesterday():
    assert resolve_time_window("what did I do yesterday", now_ms=NOW, tz=UTC) == (
        _ms(2026, 7, 9), _ms(2026, 7, 10),
    )


def test_today_clamps_to_now():
    assert resolve_time_window("how much time today", now_ms=NOW, tz=UTC) == (
        _ms(2026, 7, 10), NOW,
    )


def test_this_morning_full_window_after_noon():
    # Asked at 14:30, the whole morning [05:00, 12:00) is in the past.
    assert resolve_time_window("this morning", now_ms=NOW, tz=UTC) == (
        _ms(2026, 7, 10, 5), _ms(2026, 7, 10, 12),
    )


def test_this_morning_at_2am_returns_none():
    # KTD3: the period hasn't started (05:00 > 02:00) → empty → no window.
    early = _ms(2026, 7, 10, 2, 0)
    assert resolve_time_window("this morning", now_ms=early, tz=UTC) is None


def test_this_afternoon_clamps_end_to_now():
    # afternoon [12:00, 17:00) but now is 14:30 → clamped.
    assert resolve_time_window("this afternoon", now_ms=NOW, tz=UTC) == (
        _ms(2026, 7, 10, 12), NOW,
    )


def test_yesterday_evening():
    assert resolve_time_window("what did I see yesterday evening", now_ms=NOW, tz=UTC) == (
        _ms(2026, 7, 9, 17), _ms(2026, 7, 10),  # 17:00 → midnight
    )


def test_this_week_starts_monday():
    now_dt = dt.datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    monday = (now_dt - dt.timedelta(days=now_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert resolve_time_window("this week", now_ms=NOW, tz=UTC) == (
        int(monday.timestamp() * 1000), NOW,
    )


def test_last_week():
    now_dt = dt.datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    monday = (now_dt - dt.timedelta(days=now_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_monday = monday - dt.timedelta(weeks=1)
    assert resolve_time_window("last week", now_ms=NOW, tz=UTC) == (
        int(last_monday.timestamp() * 1000), int(monday.timestamp() * 1000),
    )


def test_last_n_hours_rolling():
    assert resolve_time_window("last 3 hours", now_ms=NOW, tz=UTC) == (
        NOW - 3 * 3600 * 1000, NOW,
    )


def test_this_month():
    assert resolve_time_window("what did I do this month", now_ms=NOW, tz=UTC) == (
        _ms(2026, 7, 1), NOW,
    )


@pytest.mark.parametrize(
    "question",
    [
        "what was that error I saw",   # a specific lookup, no time phrase
        "which site had the login bug",
        "",
        "find the invoice number",
    ],
)
def test_unrecognized_phrase_returns_none(question):
    assert resolve_time_window(question, now_ms=NOW, tz=UTC) is None


def test_out_of_range_last_n_degrades_to_none_never_raises():
    # A recognized phrase with an absurd magnitude overflows timedelta; it must
    # degrade to no-window (not raise), so the fail-safe chat.answer verb never 500s.
    assert resolve_time_window(
        "what happened in the last 999999999999999999 days", now_ms=NOW, tz=UTC
    ) is None
    # "last 0 hours" clamps to an empty window → None (not a crash, not a bad window).
    assert resolve_time_window("last 0 hours", now_ms=NOW, tz=UTC) is None
