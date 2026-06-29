"""Re-derive the ``SCRUB_BLOCK_ACTIONS`` skip set for a finished recording (U2).

The backfill (SCR-178) OCR-indexes *existing* recordings into the local content
index, and must skip exactly the frames the live per-chunk path would skip —
ALLOW frames only, never a ``SCRUB_BLOCK_ACTIONS`` (EXCLUDE / MASK_WINDOW /
MASK_REGION / TEXT_REDACT / OCR_FALLBACK) or secure-field interval.

No reusable on-disk block-interval data exists in the correct form (persisted
manifest ``blocked_intervals`` are the capture-time EXCLUDE-only set). So this
module **re-derives** the canonical skip set from each recording's local
``recording.db``.

Corrected premise (verified during plan/doc review — do NOT re-introduce the
nulled-column model)
---------------------------------------------------------------------------
The local ``~/.screencap/recordings/<name>/recording.db`` the backfill reads is
**INTACT**. ``_null_db_rows_for_intervals`` (which nulls ``title`` / ``state`` /
``browser_url`` / ``element_state``) runs **only** inside ``Scrubber.run()`` →
``scrub_recording``, which operates on a disposable ``<name>-scrubbed/`` *copy*
and never mutates the original. The in-place chunk scrub never nulls the DB.
The **one** path that mutates the local DB is
``enforcement/scrub_worker.py`` on a retroactive "disable this app": it
**DELETES whole ``window_event`` / ``action_event`` rows and unlinks the
matching ``screenshots/*.jpg``** — it does not null columns.

Consequence: re-deriving over the intact DB reproduces the canonical
``SCRUB_BLOCK_ACTIONS`` skip set with **no signal loss**. The genuine fail-closed
residual this module adds on top is:

1. **Uncovered-gap intervals** — a screenshot timestamp with NO covering
   ``window_event`` (a retroactive deletion removed the rows). Such frames are
   usually already unlinked, but if an orphan survives we cannot prove it
   ALLOW, so we skip it, biased OUTWARD so an adjacent ALLOW window's
   classification can't leak across the boundary.

2. **Ambiguity intervals** — a window whose classification genuinely depends on
   a policy-relevant column that is NULL (``browser_url`` on a known browser
   under a URL-routed policy, ``title`` under a title-dependent policy) or an
   ``element_state IS NULL`` span where a secure field could have been. These
   are detected via **raw SQL on the columns** — NOT the ``WindowContext``
   loader, which coerces ``title`` NULL → ``''`` and would destroy the NULL
   distinction. ``app_bundle_id`` is never nulled, so bundle-id-classified apps
   (password managers, banking) need no ambiguity handling.

Tightest-safe column-dependency predicate
-----------------------------------------
"Is this classification column-dependent?" is decided from the *actual*
classifier behaviour, not a hardcoded guess: classify the window with the real
(NULL) column, and again with one or more probe values. A NULL column is treated
as ambiguous iff the real classification is NOT already in
``SCRUB_BLOCK_ACTIONS`` *and* some probe value DOES land in
``SCRUB_BLOCK_ACTIONS``. (If the real classification is already blocked, the
canonical pass covers it.) This is the tightest skip that stays a superset of
the live block set.

Over-skip tradeoff
------------------
The returned set is always a **superset** of (never a subset of) what
current-policy live scrubbing would block — it may over-skip some
legitimately-allowed frames at a coverage gap or a NULL column. That direction
matches the codebase's fail-closed masking posture: better to miss indexing a
benign frame than to index masked-app on-screen text.

Parity is "identical given identical config": re-derivation reflects the
classifier / evaluator built for the recording's ``PrivacyMode``. The mode is
NOT taken from the mutable current config — it is the **capture-time** mode,
frozen into ``.recording_intent`` (``privacy_mode``) at start time and read back
by ``_resolve_mode`` (cloud/both force ``PUBLIC``; local reads the frozen mode;
absent/unparseable → ``PUBLIC`` fail-closed). So a global ``PrivacyMode``
*relaxed since capture* can no longer make the re-derived set block LESS than
capture did (SCR-190 — closes the prior relaxed-since-capture under-block hole).
If the rest of the privacy config (app classes, exclude lists) changed since
capture the re-derived set may still block more (acceptable) — never less.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from screencap.privacy.actions import SCRUB_BLOCK_ACTIONS, PrivacyAction
from screencap.privacy.policy import (
    DEFAULT_TRANSITION_HOLD_SECONDS,
    FrameMetadata,
    PrivacyMode,
)
from screencap.recording_db import has_column, has_table, open_recording_db
from screencap.scrubber import (
    BlockedInterval,
    build_scrub_context,
    merge_intervals,
)

if TYPE_CHECKING:
    from screencap.privacy.classify import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator

logger = logging.getLogger(__name__)

# Reason codes for the fail-closed residual intervals this module adds on top of
# the canonical scrub set. Backfill-local (not in ``privacy.reasons.ReasonCode``)
# because they describe a re-derivation artifact, not a policy decision.
UNCOVERED_GAP = "uncovered_gap"
AMBIGUOUS_BROWSER_URL = "ambiguous_browser_url"
AMBIGUOUS_TITLE = "ambiguous_title"
AMBIGUOUS_SECURE_FIELD = "ambiguous_secure_field"

# How far OUTWARD a single-screenshot uncovered gap is padded so an adjacent
# ALLOW window's classification cannot leak across the boundary, and so a frame
# a hair before/after the orphan is still inside the skip. Sized to comfortably
# exceed the screenshot cadence; the exact value is not load-bearing (the
# interval only ever drops frames from indexing).
_GAP_PAD_SECONDS = 5.0

# Hold window applied to an ``element_state IS NULL`` span mirrors
# ``DEFAULT_TRANSITION_HOLD_SECONDS`` (imported above), used by
# ``build_secure_field_intervals`` for intact AXSecureTextField spans.

# Probe values used to decide whether a NULL column is classification-relevant.
# A URL/title that, if present, routes the window into a blocked action under
# the active policy. We probe a small set spanning the sensitive classes so the
# predicate fires whenever *any* value of the null column could block.
_URL_PROBES = (
    "https://www.chase.com/login",   # banking / auth
    "https://mail.google.com/mail",  # email
    "https://checkout.stripe.com/x",  # payment
)
_TITLE_PROBES = (
    "Bank Login",       # auth_flow / banking
    "Inbox — Mail",     # email
)


# ---------------------------------------------------------------------------
# Classifier + evaluator constructor (mode per .recording_intent)
# ---------------------------------------------------------------------------


def build_classifier_evaluator(
    recording_dir: Path | None = None,
    *,
    mode: PrivacyMode | None = None,
) -> tuple[DefaultContextClassifier, DefaultPolicyEvaluator]:
    """Build the (classifier, evaluator) the re-derivation uses.

    Mirrors ``ChunkScrubber._build``: starts from ``get_privacy_config()`` and
    applies the ``PrivacyMode.PUBLIC`` override for cloud-intent recordings.

    Mode selection (shared by U4 and the tests):
      * explicit ``mode=`` wins (test / caller override),
      * else read the recording's frozen ``.recording_intent``: cloud/both
        destination → ``PUBLIC`` (matching ``ChunkScrubber._build``'s
        ``cloud_intent`` branch); local-only → the frozen **capture-time**
        ``privacy_mode`` (SCR-190 — never the mutable current config),
      * else (``.recording_intent`` absent/unreadable, or a local recording
        with no/unparseable frozen ``privacy_mode``) → ``PUBLIC``, the
        strictest fail-closed default.

    Returns ``(DefaultContextClassifier, DefaultPolicyEvaluator)``.
    """
    from dataclasses import replace as _dc_replace

    from screencap.config import get_privacy_config
    from screencap.privacy.classify import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator

    pc = get_privacy_config()

    resolved_mode = mode if mode is not None else _resolve_mode(recording_dir)
    if resolved_mode != pc.mode:
        pc = _dc_replace(pc, mode=resolved_mode)

    evaluator = DefaultPolicyEvaluator(pc)
    classifier = DefaultContextClassifier(app_classes=pc.app_classes)
    return classifier, evaluator


def _resolve_mode(recording_dir: Path | None) -> PrivacyMode:
    """Resolve the PrivacyMode for a recording from its frozen ``.recording_intent``.

    cloud/both → ``PUBLIC``; local → the frozen **capture-time** ``privacy_mode``
    (SCR-190); absent/unreadable/unparseable → ``PUBLIC`` (fail-closed).

    Local recordings read the frozen capture-time mode — NOT the mutable current
    ``get_privacy_config().mode`` — so a global mode *relaxed since capture* can
    never make the re-derived skip set block less than capture-time did. If the
    frozen ``privacy_mode`` is absent (legacy intent) or unparseable we fail
    closed to ``PUBLIC`` rather than trusting the (possibly relaxed) global.
    """
    if recording_dir is None:
        return PrivacyMode.PUBLIC

    from screencap.catalog import read_intent, read_intent_privacy_mode

    try:
        destination = read_intent(Path(recording_dir))
    except Exception:
        logger.debug("Failed to read .recording_intent; defaulting to PUBLIC", exc_info=True)
        return PrivacyMode.PUBLIC

    if destination in ("cloud", "both"):
        return PrivacyMode.PUBLIC
    if destination == "local":
        # Use the frozen capture-time mode. Fail closed to PUBLIC when it is
        # absent/unparseable — never fall back to the mutable current config.
        frozen = read_intent_privacy_mode(Path(recording_dir))
        if frozen is not None:
            try:
                return PrivacyMode(frozen)
            except ValueError:
                logger.debug(
                    "Unparseable frozen privacy_mode %r; defaulting to PUBLIC", frozen,
                )
        return PrivacyMode.PUBLIC
    # Missing / unknown destination → strictest fail-closed default.
    return PrivacyMode.PUBLIC


# ---------------------------------------------------------------------------
# Skip-interval derivation
# ---------------------------------------------------------------------------


def derive_skip_intervals(
    db_path: Path | str,
    *,
    classifier,
    evaluator,
    time_range: tuple[float, float],
    screenshot_timestamps: list[float] | None = None,
) -> list[BlockedInterval]:
    """Return the merged/sorted union of skip intervals for ``[start, end)``.

    The union is:

    1. **Canonical** ``SCRUB_BLOCK_ACTIONS`` intervals from
       ``build_scrub_context`` (already includes secure-field intervals).
    2. **Uncovered-gap** intervals — for each timestamp in
       ``screenshot_timestamps`` with no covering ``window_event`` (fail-closed).
       Omit ``screenshot_timestamps`` (or pass ``None``) to skip this pass — the
       backfill engine (U4) supplies the flat ``screenshots/*.jpg`` timestamps.
    3. **Ambiguity** intervals — NULL ``browser_url`` / ``title`` / ``element_state``
       spans whose classification genuinely depends on the null column
       (fail-closed; detected via raw SQL on the columns).

    Always a superset of the live block set; never raises on a legacy schema or
    a missing/locked DB (returns whatever it could derive, fail-closed).
    """
    db_path = Path(db_path)
    start, end = time_range

    canonical: list[BlockedInterval] = []
    ambiguity: list[BlockedInterval] = []
    window_starts: list[float] = []

    db_exists = db_path.is_file()
    if db_exists:
        # 1. Canonical SCRUB_BLOCK_ACTIONS intervals (incl. secure-field).
        try:
            # build_scrub_context already builds its blocked_intervals with
            # ``actions=SCRUB_BLOCK_ACTIONS`` internally (the scrub-time set) and
            # merges secure-field intervals — no actions kwarg to pass.
            ctx = build_scrub_context(
                db_path, evaluator, classifier, time_range=time_range,
            )
            canonical = list(ctx.blocked_intervals)
        except Exception:
            logger.warning(
                "derive_skip_intervals: canonical build_scrub_context failed; "
                "relying on gap/ambiguity residual only", exc_info=True,
            )

        # 3. Ambiguity intervals (raw SQL on the columns).
        try:
            ambiguity, window_starts = _derive_ambiguity_intervals(
                db_path, classifier, evaluator, time_range,
            )
        except Exception:
            logger.warning(
                "derive_skip_intervals: ambiguity derivation failed", exc_info=True,
            )

    # 2. Uncovered-gap intervals (orphan screenshots with no covering window).
    gaps: list[BlockedInterval] = []
    if screenshot_timestamps:
        gaps = _derive_uncovered_gaps(
            screenshot_timestamps, window_starts, start, end,
        )

    return merge_intervals(canonical, ambiguity, gaps)


def _derive_uncovered_gaps(
    screenshot_timestamps: list[float],
    window_starts: list[float],
    range_start: float,
    range_end: float,
) -> list[BlockedInterval]:
    """Skip every in-range screenshot with no covering ``window_event``.

    Window events are state transitions: a surviving event at ``T`` describes
    the active window for the whole half-open span ``[T, next_event_ts)`` (the
    last surviving event runs to ``+inf``). A screenshot is **covered** iff it
    falls in some such span — i.e. iff *some* surviving event lies at or before
    it. The genuinely uncovered cases this catches are a screenshot **before the
    first** surviving event (the covering rows were retroactively deleted) and a
    recording with **no** surviving window events at all. We cannot prove such a
    frame ALLOW → skip it, padded OUTWARD so the boundary can't leak the
    classification of the nearest surviving window.

    Note this is interval-coverage, not nearest-within-delta: a frame long after
    a surviving event but still inside its (open-ended) span is covered by that
    event's canonical/ambiguity interval, so it is not an uncovered gap.
    """
    sorted_starts = sorted(window_starts)
    earliest = sorted_starts[0] if sorted_starts else None
    out: list[BlockedInterval] = []
    for ts in screenshot_timestamps:
        if not (range_start <= ts < range_end):
            continue
        # Covered iff some surviving event lies at or before this screenshot.
        if earliest is not None and ts >= earliest:
            continue
        out.append(BlockedInterval(
            start=ts - _GAP_PAD_SECONDS,
            end=ts + _GAP_PAD_SECONDS,
            action=PrivacyAction.EXCLUDE,
            reason=UNCOVERED_GAP,
        ))
    return out


def _derive_ambiguity_intervals(
    db_path: Path,
    classifier,
    evaluator,
    time_range: tuple[float, float],
) -> tuple[list[BlockedInterval], list[float]]:
    """Raw-SQL pass for NULL-column classification ambiguity.

    Returns ``(ambiguity_intervals, window_event_timestamps)``. The second
    value is the surviving window-event timestamps in range (used by the
    uncovered-gap pass), read here to avoid a second DB open.
    """
    start, end = time_range
    intervals: list[BlockedInterval] = []
    window_starts: list[float] = []

    with open_recording_db(db_path) as conn:
        # Computed once and reused below (it is irrelevant when there are no
        # window rows). Avoids a second has_table+has_column round-trip.
        has_url = has_table(conn, "window_event") and has_column(
            conn, "window_event", "browser_url"
        )
        if not has_table(conn, "window_event"):
            # No window table — still surface action_event ambiguity below.
            window_rows = []
        else:
            # Include the last event before range start (initial context),
            # matching build_scrub_context's scoped load.
            cols = "timestamp, app_bundle_id, title" + (", browser_url" if has_url else "")
            before = conn.execute(
                f"SELECT {cols} FROM window_event "
                "WHERE timestamp IS NOT NULL AND timestamp < ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (start,),
            ).fetchall()
            inside = conn.execute(
                f"SELECT {cols} FROM window_event "
                "WHERE timestamp IS NOT NULL AND timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp",
                (start, end),
            ).fetchall()
            window_rows = list(before) + list(inside)

        # Each window event spans [its ts, next event's ts) — the same interval
        # model build_blocked_intervals uses.
        n = len(window_rows)
        for i, row in enumerate(window_rows):
            ts = float(row[0])
            window_starts.append(ts)
            iv_start = ts
            iv_end = float(window_rows[i + 1][0]) if i + 1 < n else float("inf")

            bundle = row[1]  # may be None
            raw_title = row[2]  # raw — NULL preserved (NOT coerced to '')
            raw_url = row[3] if has_url else None

            amb = _classify_ambiguity(
                classifier, evaluator,
                bundle=bundle, raw_title=raw_title, raw_url=raw_url,
            )
            if amb is not None:
                intervals.append(BlockedInterval(
                    start=iv_start, end=iv_end,
                    action=PrivacyAction.EXCLUDE, reason=amb,
                ))

        # element_state IS NULL spans — a secure field could have been there.
        if has_table(conn, "action_event") and has_column(
            conn, "action_event", "element_state"
        ):
            null_rows = conn.execute(
                "SELECT timestamp FROM action_event "
                "WHERE element_state IS NULL AND timestamp IS NOT NULL "
                "AND timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp",
                (start, end),
            ).fetchall()
            for (ts,) in null_rows:
                intervals.append(BlockedInterval(
                    start=float(ts),
                    end=float(ts) + DEFAULT_TRANSITION_HOLD_SECONDS,
                    action=PrivacyAction.EXCLUDE,
                    reason=AMBIGUOUS_SECURE_FIELD,
                ))

    return intervals, window_starts


def _classify_ambiguity(
    classifier,
    evaluator,
    *,
    bundle: str | None,
    raw_title: str | None,
    raw_url: str | None,
) -> str | None:
    """Decide whether a NULL column makes this window's classification ambiguous.

    Tightest-safe predicate: a column counts as ambiguous iff it is genuinely
    NULL *and* the actual (null) classification is NOT already blocked while
    some probe value of that column WOULD land in ``SCRUB_BLOCK_ACTIONS``. If the
    actual classification is already blocked, the canonical pass covers it — no
    ambiguity interval needed. ``browser_url`` is checked before ``title`` so a
    browser's url-ambiguity is attributed to the url (the routing column).
    """
    bundle = bundle or ""

    # The actual (real-column) decision. If already blocked, canonical covers it.
    actual = _evaluate(classifier, evaluator, bundle, raw_title, raw_url)
    if actual in SCRUB_BLOCK_ACTIONS:
        return None

    # browser_url NULL on a known browser whose routing depends on the URL.
    if raw_url is None and _is_known_browser(classifier, bundle):
        for probe in _URL_PROBES:
            if _evaluate(classifier, evaluator, bundle, raw_title, probe) in SCRUB_BLOCK_ACTIONS:
                return AMBIGUOUS_BROWSER_URL

    # title NULL whose classification could route on the title.
    if raw_title is None:
        for probe in _TITLE_PROBES:
            if _evaluate(classifier, evaluator, bundle, probe, raw_url) in SCRUB_BLOCK_ACTIONS:
                return AMBIGUOUS_TITLE

    return None


def _evaluate(
    classifier, evaluator, bundle: str, title: str | None, url: str | None,
) -> PrivacyAction:
    """Classify + evaluate a synthetic frame, mirroring build_blocked_intervals.

    ``title``/``url`` may be the raw NULL (``None``) — we coerce title to ``''``
    for ``FrameMetadata`` exactly as the live path does, but the *decision to
    probe* was already made on the raw NULL upstream.
    """
    from screencap.privacy.classify import domain_from_url

    domain = domain_from_url(url) if url else None
    meta = FrameMetadata(
        bundle_id=bundle,
        window_title=title or "",
        domain=domain,
        timestamp=0.0,
        browser_url=url or None,
    )
    ctx = classifier.classify(meta)
    return evaluator.evaluate(ctx, meta).action


def _is_known_browser(classifier, bundle: str) -> bool:
    """True iff ``bundle`` is a known browser (hardcoded set or user-tagged).

    Mirrors ``DefaultContextClassifier.classify`` step 3's browser test so the
    url-ambiguity probe only fires for windows whose classification actually
    routes on the URL.
    """
    if not bundle:
        return False
    from screencap.privacy.classify import BROWSER_BUNDLE_IDS
    from screencap.privacy.policy import ContextClass

    if bundle in BROWSER_BUNDLE_IDS:
        return True
    app_classes = getattr(classifier, "_app_classes", {}) or {}
    return app_classes.get(bundle) == ContextClass.BROWSER_UNVERIFIED
