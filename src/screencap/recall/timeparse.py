"""Dependency-free natural-language time-window resolver (U3).

Maps a bounded set of common phrases in a chat question — "today", "yesterday",
"this morning", "this week", "last week", "last N hours/days", "this month" — to a
half-open ``(start_ms, end_ms)`` window in Unix epoch milliseconds, so a chat turn
can scope retrieval (point questions) or compute figures (aggregate questions) over
the period the operator asked about.

It runs daemon-side (the ``chat.answer`` handler calls it when the client sent no
explicit ``window_ms``), so the MCP ``chat.answer`` tool benefits from time-scoping
too — not just the macOS app. No NLP dependency is added; the phrase set is a small
hand-rolled matcher, intentionally conservative: an unrecognized phrase returns
``None`` (the caller falls back to no-window), never a guess.

**Window-boundary rule (KTD3).** Windows are clamped so a phrase never yields a
future or empty window: the end is clamped to ``now`` (asking "this morning" at
02:00 does not return a window ending at noon), and a window that clamps to
zero-or-negative width returns ``None`` (same as an unrecognized phrase) rather than
a silently-empty window that would read as "no matching moments".
"""

from __future__ import annotations

import datetime as _dt
import re

# Local-clock parts-of-day boundaries as [start_hour, end_hour). Evening runs to
# midnight (24). These are conventions, not configuration.
_MORNING = (5, 12)
_AFTERNOON = (12, 17)
_EVENING = (17, 24)
_PARTS = (("morning", _MORNING), ("afternoon", _AFTERNOON), ("evening", _EVENING))

# "last 3 hours" / "last 2 days" / "last 1 week" — rolling windows ending at now.
_LAST_N_RE = re.compile(r"\blast\s+(\d+)\s+(hour|hours|day|days|week|weeks)\b")


def resolve_time_window(
    question: str,
    *,
    now_ms: int,
    tz: _dt.tzinfo | None = None,
) -> tuple[int, int] | None:
    """Resolve a time reference in ``question`` to a half-open ``(start_ms, end_ms)``.

    ``now_ms`` is the current wall clock (the daemon passes ``time.time()*1000``;
    tests pass a fixed value). ``tz`` defaults to the host's local timezone. Returns
    ``None`` when no supported phrase is present or the window clamps empty.
    """
    q = (question or "").lower()
    if tz is None:
        tz = _dt.datetime.now().astimezone().tzinfo
    now = _dt.datetime.fromtimestamp(now_ms / 1000.0, tz=tz)
    midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _ms(d: _dt.datetime) -> int:
        return int(d.timestamp() * 1000)

    def _window(start: _dt.datetime, end: _dt.datetime) -> tuple[int, int] | None:
        # Half-open [start, end); clamp end to now (never a future window); a
        # zero-or-negative width clamps to None (KTD3).
        end = min(end, now)
        if end <= start:
            return None
        return (_ms(start), _ms(end))

    def _part_of(day_start: _dt.datetime, hours: tuple[int, int]) -> tuple[_dt.datetime, _dt.datetime]:
        h0, h1 = hours
        start = day_start.replace(hour=h0)
        end = day_start + _dt.timedelta(days=1) if h1 == 24 else day_start.replace(hour=h1)
        return start, end

    # "last N hours/days/weeks" — rolling window ending now.
    m = _LAST_N_RE.search(q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("hour"):
            delta = _dt.timedelta(hours=n)
        elif unit.startswith("day"):
            delta = _dt.timedelta(days=n)
        else:
            delta = _dt.timedelta(weeks=n)
        return _window(now - delta, now)
    if "last hour" in q:
        return _window(now - _dt.timedelta(hours=1), now)

    # "yesterday morning/afternoon/evening" (before the bare "yesterday" and
    # "this <part>" checks, so the more-specific phrase wins).
    yesterday_midnight = midnight_today - _dt.timedelta(days=1)
    for name, hours in _PARTS:
        if f"yesterday {name}" in q:
            start, end = _part_of(yesterday_midnight, hours)
            return _window(start, end)
    # "this morning/afternoon/evening".
    for name, hours in _PARTS:
        if f"this {name}" in q:
            start, end = _part_of(midnight_today, hours)
            return _window(start, end)

    if "yesterday" in q:
        return _window(yesterday_midnight, midnight_today)
    if "today" in q:
        return _window(midnight_today, now)

    # Weeks (ISO: week starts Monday).
    this_monday = midnight_today - _dt.timedelta(days=midnight_today.weekday())
    if "last week" in q:
        return _window(this_monday - _dt.timedelta(weeks=1), this_monday)
    if "this week" in q:
        return _window(this_monday, now)

    # Months.
    first_of_month = midnight_today.replace(day=1)
    if "last month" in q:
        first_of_prev = (first_of_month - _dt.timedelta(days=1)).replace(day=1)
        return _window(first_of_prev, first_of_month)
    if "this month" in q:
        return _window(first_of_month, now)

    return None
