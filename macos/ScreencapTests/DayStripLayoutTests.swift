import XCTest
@testable import Screencap

/// U9/U1 — the day strip's pure geometry: the fixed-waking-window axis bounds
/// (08:00–21:00, extended outward for out-of-window footage, DST-safe day
/// clamping), footage boundary labels, time→x mapping, marker clustering, the
/// R7 blocked-band mapping, and the accessibility label builders.
final class DayStripLayoutTests: XCTestCase {

    private let hour = DayStripLayout.hourMs
    /// An arbitrary local-midnight origin; the layout only does arithmetic on it.
    private let dayStart = 1_800_000_000_000
    /// The DST-safe day end the view now passes; a synthetic 24h day here.
    private var dayEnd: Int { dayStart + 24 * hour }

    // MARK: - U4 landing highlight (AE3)

    /// An exact-endpoints task span resolves to that band (the common AE3 case:
    /// Tasks/Chat hands the task's own span).
    func testResolveHighlightSpanPrefersExactTaskMatch() {
        let segments = [
            (startMs: dayStart, endMs: dayStart + hour),
            (startMs: dayStart + hour, endMs: dayStart + 2 * hour),
        ]
        let resolved = DayStripLayout.resolveHighlightSpan(
            highlight: (startMs: dayStart + hour, endMs: dayStart + 2 * hour), segments: segments
        )
        XCTAssertEqual(resolved.startMs, dayStart + hour)
        XCTAssertEqual(resolved.endMs, dayStart + 2 * hour)
    }

    /// A highlight whose endpoints don't match but whose midpoint sits inside a
    /// band emphasizes that containing band.
    func testResolveHighlightSpanFallsBackToContainingBand() {
        let segments = [(startMs: dayStart, endMs: dayStart + 2 * hour)]
        let resolved = DayStripLayout.resolveHighlightSpan(
            highlight: (startMs: dayStart + 30 * 60_000, endMs: dayStart + 90 * 60_000),
            segments: segments
        )
        XCTAssertEqual(resolved.startMs, dayStart)
        XCTAssertEqual(resolved.endMs, dayStart + 2 * hour, "lands on the containing task band")
    }

    /// No task band matches (unsplit footage, or bands not loaded yet) → outline
    /// the raw highlight span, never drop it.
    func testResolveHighlightSpanFallsBackToRawSpanWhenNoBandMatches() {
        let hl = (startMs: dayStart + 5 * hour, endMs: dayStart + 6 * hour)
        let resolved = DayStripLayout.resolveHighlightSpan(highlight: hl, segments: [])
        XCTAssertEqual(resolved.startMs, hl.startMs)
        XCTAssertEqual(resolved.endMs, hl.endMs)
    }

    // MARK: - Axis bounds (fixed waking window, R6)

    /// The founding fix (AE1): a short afternoon recording on an otherwise
    /// empty day renders on the full 08:00–21:00 waking axis, positioned in
    /// early afternoon — not zoomed to fill, which read as "a few minutes".
    func testAxisBoundsInWindowFootageKeepsFixedWakingWindow() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, dayEndMs: dayEnd, spans: [
            (startMs: dayStart + 13 * hour + 53 * 60_000, endMs: dayStart + 14 * hour + 44 * 60_000),
        ])
        XCTAssertEqual(bounds.startMs, dayStart + 8 * hour, "axis starts at 08:00 regardless of footage")
        XCTAssertEqual(bounds.endMs, dayStart + 21 * hour, "axis ends at 21:00 regardless of footage")
    }

    func testAxisBoundsSpanlessIsWakingWindow() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, dayEndMs: dayEnd, spans: [])
        XCTAssertEqual(bounds.startMs, dayStart + 8 * hour)
        XCTAssertEqual(bounds.endMs, dayStart + 21 * hour)
    }

    /// Footage before 08:00 extends the axis start outward (hour-floored).
    func testAxisBoundsExtendsStartForEarlyFootage() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, dayEndMs: dayEnd, spans: [
            (startMs: dayStart + 6 * hour + 20 * 60_000, endMs: dayStart + 9 * hour),
        ])
        XCTAssertEqual(bounds.startMs, dayStart + 6 * hour, "start floors to the hour below the footage")
        XCTAssertEqual(bounds.endMs, dayStart + 21 * hour, "end stays at the waking-window close")
    }

    /// Footage after 21:00 extends the axis end outward (hour-ceiled): the
    /// plan's "22:30 footage extends the axis to 23:00".
    func testAxisBoundsExtendsEndForLateFootage() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, dayEndMs: dayEnd, spans: [
            (startMs: dayStart + 20 * hour, endMs: dayStart + 22 * hour + 30 * 60_000),
        ])
        XCTAssertEqual(bounds.startMs, dayStart + 8 * hour, "start stays at the waking-window open")
        XCTAssertEqual(bounds.endMs, dayStart + 23 * hour, "end ceils to the hour above the footage")
    }

    /// A client-side span leaking past the day still clamps into it (both edges).
    func testAxisBoundsClampIntoTheDay() {
        let bounds = DayStripLayout.axisBounds(dayStartMs: dayStart, dayEndMs: dayEnd, spans: [
            (startMs: dayStart - 2 * hour, endMs: dayStart + 25 * hour),
        ])
        XCTAssertEqual(bounds.startMs, dayStart)
        XCTAssertEqual(bounds.endMs, dayStart + 24 * hour)
    }

    /// DST-safe: the clamp uses the passed day end, not a hardcoded +24h. On a
    /// 25h "fall back" day, late footage clamps to the real (longer) day end;
    /// a spanless short/long day still shows the fixed waking window.
    func testAxisBoundsClampsToPassedDayEndForDstDay() {
        let longDayEnd = dayStart + 25 * hour
        let late = DayStripLayout.axisBounds(dayStartMs: dayStart, dayEndMs: longDayEnd, spans: [
            (startMs: dayStart + 23 * hour, endMs: dayStart + 24 * hour + 30 * 60_000),
        ])
        XCTAssertEqual(late.endMs, dayStart + 25 * hour, "end ceils but clamps to the real 25h day end")
        let shortSpanless = DayStripLayout.axisBounds(
            dayStartMs: dayStart, dayEndMs: dayStart + 23 * hour, spans: []
        )
        XCTAssertEqual(shortSpanless.startMs, dayStart + 8 * hour)
        XCTAssertEqual(shortSpanless.endMs, dayStart + 21 * hour)
    }

    // MARK: - Footage boundary labels (AE1)

    /// The first footage start and last footage end inside the axis get an
    /// honest "recording started/stopped HH:MM" edge label; a spanless day has
    /// none.
    func testFootageBoundaryLabels() {
        let start = dayStart + 13 * hour + 53 * 60_000
        let end = dayStart + 14 * hour + 44 * 60_000
        let labels = DayStripLayout.footageBoundaryLabels(spans: [(startMs: start, endMs: end)])
        XCTAssertEqual(labels.count, 2)
        XCTAssertEqual(labels[0].ms, start)
        XCTAssertEqual(labels[0].kind, .started)
        XCTAssertEqual(labels[1].ms, end)
        XCTAssertEqual(labels[1].kind, .stopped)
        XCTAssertTrue(DayStripLayout.footageBoundaryLabels(spans: []).isEmpty)
    }

    // MARK: - Time → x

    func testTimeToXMapsLinearlyAndClamps() {
        let bounds = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 10 * hour)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart, bounds: bounds, width: 500), 0)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart + 5 * hour, bounds: bounds, width: 500), 250)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart + 10 * hour, bounds: bounds, width: 500), 500)
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart - hour, bounds: bounds, width: 500), 0, "clamps below")
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart + 11 * hour, bounds: bounds, width: 500), 500, "clamps above")
        XCTAssertEqual(DayStripLayout.x(forMs: dayStart, bounds: bounds, width: 0), 0, "zero width → no NaN")
    }

    func testXToMsRoundTrips() {
        let bounds = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 8 * hour)
        let ms = dayStart + 3 * hour
        let x = DayStripLayout.x(forMs: ms, bounds: bounds, width: 640)
        XCTAssertEqual(DayStripLayout.ms(forX: x, bounds: bounds, width: 640), ms)
        XCTAssertEqual(DayStripLayout.ms(forX: -10, bounds: bounds, width: 640), bounds.startMs, "clamps below")
        XCTAssertEqual(DayStripLayout.ms(forX: 900, bounds: bounds, width: 640), bounds.endMs, "clamps above")
    }

    func testHourTicksStepWidensPastTwelveHours() {
        let short = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 8 * hour)
        XCTAssertEqual(DayStripLayout.hourTicks(bounds: short).count, 5, "8h span → 2h steps")
        let full = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 24 * hour)
        XCTAssertEqual(DayStripLayout.hourTicks(bounds: full).count, 7, "24h span → 4h steps")
    }

    // MARK: - Marker clustering

    /// Same single-linkage contract as the retired SearchTimelineLayout's
    /// cluster: chained neighbors within the threshold collapse to their mean x.
    func testMarkerClusteringChainsWithinThreshold() {
        XCTAssertEqual(DayStripLayout.clusterXs([10, 20, 30, 200], thresholdPx: 14), [20, 200])
        XCTAssertEqual(DayStripLayout.clusterXs([5], thresholdPx: 14), [5])
        XCTAssertEqual(DayStripLayout.clusterXs([], thresholdPx: 14), [])
        XCTAssertEqual(
            DayStripLayout.clusterXs([30, 10], thresholdPx: 0), [10, 30],
            "non-positive threshold disables clustering but still sorts"
        )
    }

    // MARK: - Task-label placement

    /// Greedy left-to-right: a label whose band starts within 8pt of the
    /// previous label's end is skipped (nil), later bands still place.
    func testTaskLabelFramesSkipOverlapsGreedily() {
        let frames = DayStripLayout.taskLabelFrames(
            bands: [(minX: 0, width: 100), (minX: 30, width: 100), (minX: 200, width: 60)],
            measuredWidths: [50, 50, 40]
        )
        XCTAssertEqual(frames.count, 3, "output stays aligned with the input bands")
        XCTAssertEqual(frames[0], DayStripLayout.TaskLabelFrame(minX: 0, width: 50, truncated: false))
        XCTAssertNil(frames[1], "band starting inside the previous label + 8pt spacing is skipped")
        XCTAssertEqual(frames[2], DayStripLayout.TaskLabelFrame(minX: 200, width: 40, truncated: false))
    }

    /// A very narrow band truncates its label but never below the 44pt hint
    /// width — the label shrinks, it doesn't disappear.
    func testNarrowBandLabelTruncatesButKeepsMinimumHintWidth() {
        let frames = DayStripLayout.taskLabelFrames(
            bands: [(minX: 0, width: 10)],
            measuredWidths: [80]
        )
        XCTAssertEqual(
            frames[0], DayStripLayout.TaskLabelFrame(minX: 0, width: 44, truncated: true),
            "10pt band still yields the 44pt one-word hint"
        )
    }

    /// `truncated` is set exactly when the measured name doesn't fit the
    /// allowed width — a fitting label is never flagged.
    func testTruncatedFlagSetExactlyWhenNameDoesNotFit() {
        let frames = DayStripLayout.taskLabelFrames(
            bands: [(minX: 0, width: 100), (minX: 300, width: 100)],
            measuredWidths: [60, 120]
        )
        XCTAssertEqual(frames[0], DayStripLayout.TaskLabelFrame(minX: 0, width: 60, truncated: false))
        XCTAssertEqual(frames[1], DayStripLayout.TaskLabelFrame(minX: 300, width: 100, truncated: true))
    }

    // MARK: - Caption clustering

    /// Two blocked bands within the 50pt caption width coalesce to ONE caption
    /// (the old per-band draw overprinted them); far-apart bands keep two.
    func testNearbyBlockedCaptionsCoalesceFarOnesStaySeparate() {
        let near = DayStripLayout.captionClusters([(x: 10, cls: .blocked), (x: 40, cls: .blocked)])
        XCTAssertEqual(near, [DayStripLayout.Caption(x: 25, classes: [.blocked])],
                       "chained neighbors collapse to their mean x, one caption")
        let far = DayStripLayout.captionClusters([(x: 10, cls: .blocked), (x: 200, cls: .blocked)])
        XCTAssertEqual(far.count, 2, "bands beyond the caption width keep separate captions")
    }

    /// The overlap guard runs across ALL caption classes: a blocked band and a
    /// purged band within caption width coalesce into a single mixed-class
    /// caption, so their texts can never overprint (purged arrives in a later
    /// unit — the geometry supports it now).
    func testMixedClassCaptionsWithinThresholdCoalesce() {
        let captions = DayStripLayout.captionClusters([(x: 10, cls: .blocked), (x: 30, cls: .purged)])
        XCTAssertEqual(captions, [DayStripLayout.Caption(x: 20, classes: [.blocked, .purged])])
    }

    // MARK: - Tick label positions

    /// Hour-tick labels center on their tick from the measured width (replaces
    /// the fixed -14pt offset, wrong for any other label width).
    func testTickLabelsCenterOnTheirTick() {
        XCTAssertEqual(DayStripLayout.tickLabelMinX(tickX: 250, labelWidth: 28, stripWidth: 600), 236)
    }

    /// The first/last labels clamp inside the strip bounds instead of spilling
    /// past the edges.
    func testEdgeTickLabelsClampInsideStripBounds() {
        XCTAssertEqual(DayStripLayout.tickLabelMinX(tickX: 0, labelWidth: 28, stripWidth: 600), 0,
                       "first label clamps to the leading edge")
        XCTAssertEqual(DayStripLayout.tickLabelMinX(tickX: 600, labelWidth: 28, stripWidth: 600), 572,
                       "last label clamps to the trailing edge")
    }

    // MARK: - R7: blocked bands

    /// Hatching may claim "blocked" only for `blocked_proven` intervals —
    /// `unverifiable` intervals and plain gaps never produce a band.
    func testBlockedBandsComeOnlyFromProvenIntervals() {
        let spans = [
            DaySegmentRecording(
                name: "rec-a", startMs: dayStart, endMs: dayStart + hour,
                blockedProven: [DayBlockedInterval(startMs: dayStart + 100, endMs: dayStart + 200)],
                unverifiable: [DayBlockedInterval(startMs: dayStart + 300, endMs: dayStart + 400)]
            ),
            DaySegmentRecording(
                name: "rec-b", startMs: dayStart + 2 * hour, endMs: dayStart + 3 * hour,
                unverifiable: [DayBlockedInterval(startMs: dayStart + 2 * hour, endMs: dayStart + 3 * hour)]
            ),
        ]
        XCTAssertEqual(
            DayStripBlockedBand.provenBands(from: spans),
            [DayStripBlockedBand(startMs: dayStart + 100, endMs: dayStart + 200)]
        )
    }

    // MARK: - U5: gap → cause resolver

    /// One recording per end status; the gap that follows each maps to exactly
    /// the contract's cause: clean → nothing on file, interrupted → cut short
    /// (anchored at the span end), live → still recording, unknown/nil →
    /// can't verify (R4/R7/R11 — a live recording is never called interrupted).
    func testGapCauseMapsEachEndStatus() {
        let now = dayStart + 20 * hour
        func cause(_ endStatus: String?) -> DayStripLayout.GapCause {
            let span = DayStripLayout.SpanProvenance(
                startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: endStatus
            )
            return DayStripLayout.gapCause(
                gapStartMs: dayStart + 10 * hour, gapEndMs: dayStart + 11 * hour,
                spans: [span], storeMounted: true, provenanceReady: true, nowMs: now
            )
        }
        XCTAssertEqual(cause("clean"), .nothingOnFile)
        XCTAssertEqual(cause("interrupted"), .interrupted(aroundMs: dayStart + 10 * hour))
        XCTAssertEqual(cause("live"), .stillRecording)
        XCTAssertEqual(cause("unknown"), .cantVerify)
        XCTAssertEqual(cause(nil), .cantVerify, "older daemon (no end_status) is unprovable → can't verify")
        XCTAssertEqual(cause("something-new"), .cantVerify, "unrecognized wire status never earns a claim")
    }

    /// Future time claims NOTHING (R11): a gap at/after now resolves to no
    /// cause even when the preceding recording ended provably.
    func testGapCauseFutureClaimsNothing() {
        let now = dayStart + 10 * hour
        let span = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "interrupted"
        )
        XCTAssertEqual(
            DayStripLayout.gapCause(
                gapStartMs: now, gapEndMs: now + hour,
                spans: [span], storeMounted: true, provenanceReady: true, nowMs: now
            ),
            DayStripLayout.GapCause.none
        )
    }

    /// Load gate: while the day query hasn't returned (or failed), EVERY gap
    /// carries no cause claim — even one that would read "interrupted" once
    /// loaded.
    func testGapCauseNotLoadedClaimsNothingForEveryGap() {
        let span = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "interrupted"
        )
        XCTAssertEqual(
            DayStripLayout.gapCause(
                gapStartMs: dayStart + 10 * hour, gapEndMs: dayStart + 11 * hour,
                spans: [span], storeMounted: false, provenanceReady: false,
                nowMs: dayStart + 20 * hour
            ),
            DayStripLayout.GapCause.none
        )
    }

    /// Store gate: storeMounted == false makes every (past) empty stretch
    /// "can't verify" — never "nothing on file" — even after a clean shutdown.
    func testGapCauseStoreLockedReadsCantVerify() {
        let span = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "clean"
        )
        XCTAssertEqual(
            DayStripLayout.gapCause(
                gapStartMs: dayStart + 10 * hour, gapEndMs: dayStart + 11 * hour,
                spans: [span], storeMounted: false, provenanceReady: true,
                nowMs: dayStart + 20 * hour
            ),
            DayStripLayout.GapCause.cantVerify
        )
    }

    /// Coverage gate: coverageComplete == false (a recording with an
    /// unreadable recording.db couldn't be placed on the day) means an empty
    /// stretch is NOT proof nothing is on file — the confident "nothing on
    /// file" resolutions (clean predecessor AND leading gap) degrade to
    /// "can't verify", while an interrupted predecessor's claim stands on its
    /// own placed evidence.
    func testGapCauseIncompleteCoverageDegradesNothingOnFileToCantVerify() {
        let now = dayStart + 20 * hour
        func cause(_ endStatus: String?, spans: [DayStripLayout.SpanProvenance]) -> DayStripLayout.GapCause {
            DayStripLayout.gapCause(
                gapStartMs: dayStart + 10 * hour, gapEndMs: dayStart + 11 * hour,
                spans: spans, storeMounted: true, coverageComplete: false,
                provenanceReady: true, nowMs: now
            )
        }
        let clean = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "clean"
        )
        XCTAssertEqual(cause("clean", spans: [clean]), .cantVerify,
                       "clean predecessor no longer proves the gap empty")
        XCTAssertEqual(cause(nil, spans: []), .cantVerify,
                       "a leading gap is no longer an honest data claim")
        let interrupted = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "interrupted"
        )
        XCTAssertEqual(
            cause("interrupted", spans: [interrupted]),
            .interrupted(aroundMs: dayStart + 10 * hour),
            "the interrupted claim rests on the placed recording's own evidence"
        )
    }

    /// A leading gap (no same-day recording before it) is an honest data claim:
    /// the day query returned and holds nothing there → "nothing on file".
    func testGapCauseLeadingGapReadsNothingOnFile() {
        let span = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "clean"
        )
        XCTAssertEqual(
            DayStripLayout.gapCause(
                gapStartMs: dayStart + 8 * hour, gapEndMs: dayStart + 9 * hour,
                spans: [span], storeMounted: true, provenanceReady: true,
                nowMs: dayStart + 20 * hour
            ),
            DayStripLayout.GapCause.nothingOnFile
        )
    }

    /// With several same-day recordings before the gap, the *latest-ending*
    /// one owns the attribution: an interrupted later recording makes the gap
    /// read "cut short" anchored at ITS end, even when an earlier recording
    /// ended clean.
    func testGapCauseAttributesToLatestEndingPredecessor() {
        let earlier = DayStripLayout.SpanProvenance(
            startMs: dayStart + 7 * hour, endMs: dayStart + 8 * hour, endStatus: "clean"
        )
        let later = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "interrupted"
        )
        XCTAssertEqual(
            DayStripLayout.gapCause(
                gapStartMs: dayStart + 10 * hour, gapEndMs: dayStart + 11 * hour,
                spans: [earlier, later], storeMounted: true, provenanceReady: true,
                nowMs: dayStart + 20 * hour
            ),
            DayStripLayout.GapCause.interrupted(aroundMs: dayStart + 10 * hour)
        )
    }

    /// The reverse: a clean latest-ending predecessor makes the gap an honest
    /// "nothing on file" even though an EARLIER same-day recording was
    /// interrupted — old interruptions never leak forward past a clean end.
    func testGapCauseCleanLatestPredecessorMasksEarlierInterruption() {
        let earlier = DayStripLayout.SpanProvenance(
            startMs: dayStart + 7 * hour, endMs: dayStart + 8 * hour, endStatus: "interrupted"
        )
        let later = DayStripLayout.SpanProvenance(
            startMs: dayStart + 9 * hour, endMs: dayStart + 10 * hour, endStatus: "clean"
        )
        XCTAssertEqual(
            DayStripLayout.gapCause(
                gapStartMs: dayStart + 10 * hour, gapEndMs: dayStart + 11 * hour,
                spans: [earlier, later], storeMounted: true, provenanceReady: true,
                nowMs: dayStart + 20 * hour
            ),
            DayStripLayout.GapCause.nothingOnFile
        )
    }

    /// A gap straddling now splits so the pre-now part can carry a cause while
    /// the future part claims nothing (R11 — "still recording" is bounded to
    /// now). Fully-past and fully-future gaps pass through unsplit.
    func testSplitGapAtNowBoundsClaimsToNow() {
        let now = dayStart + 10 * hour
        let straddling = DayStripLayout.splitGapAtNow(
            startMs: dayStart + 9 * hour, endMs: dayStart + 11 * hour, nowMs: now
        )
        XCTAssertEqual(straddling.count, 2)
        XCTAssertEqual(straddling[0].startMs, dayStart + 9 * hour)
        XCTAssertEqual(straddling[0].endMs, now)
        XCTAssertEqual(straddling[1].startMs, now)
        XCTAssertEqual(straddling[1].endMs, dayStart + 11 * hour)
        let past = DayStripLayout.splitGapAtNow(
            startMs: dayStart + 8 * hour, endMs: dayStart + 9 * hour, nowMs: now
        )
        XCTAssertEqual(past.count, 1)
        let future = DayStripLayout.splitGapAtNow(
            startMs: dayStart + 11 * hour, endMs: dayStart + 12 * hour, nowMs: now
        )
        XCTAssertEqual(future.count, 1)
    }

    // MARK: - U5: hover hit targets (R13)

    /// A sliver region's hover frame expands (centered) to the 6pt minimum;
    /// an already-wide region keeps its own frame.
    func testHitFramesExpandSliversToMinimumWidth() {
        let frames = DayStripLayout.hitFrames([(minX: 100, width: 2), (minX: 200, width: 40)])
        let sliver = frames.first { $0.index == 0 }!
        XCTAssertEqual(sliver.minX, 98)
        XCTAssertEqual(sliver.width, 6)
        let wide = frames.first { $0.index == 1 }!
        XCTAssertEqual(wide.minX, 200)
        XCTAssertEqual(wide.width, 40)
    }

    /// Smaller-region-wins at boundary collisions: the returned draw order is
    /// wider-originals-first, so the smaller region renders later (on top) and
    /// wins the hover where expanded frames collide (later ZStack children win
    /// hit-testing). Ties keep input order.
    func testHitFramesOrderSmallerRegionsOnTop() {
        let frames = DayStripLayout.hitFrames([
            (minX: 0, width: 3), (minX: 4, width: 300), (minX: 500, width: 20),
        ])
        XCTAssertEqual(frames.map(\.index), [1, 2, 0], "widest drawn first, narrowest last (topmost)")
        let ties = DayStripLayout.hitFrames([(minX: 0, width: 10), (minX: 50, width: 10)])
        XCTAssertEqual(ties.map(\.index), [0, 1], "equal widths keep stable input order")
    }

    // MARK: - U5: cause strings (one home for hover + VoiceOver, R12)

    /// The dispatcher yields exactly one honest sentence per cause — and `nil`
    /// for `.none` (future / not loaded), which must claim nothing.
    func testGapCauseLabelsStateOneCauseWithTimeRange() {
        let start = dayStart, end = dayStart + hour
        let nothing = DayStripAccessibility.gapCauseLabel(.nothingOnFile, startMs: start, endMs: end)!
        XCTAssertTrue(nothing.hasPrefix("Nothing on file, "), "data claim — never 'no recording was running'")
        XCTAssertTrue(nothing.contains(" to "))
        let cut = DayStripAccessibility.gapCauseLabel(.interrupted(aroundMs: end), startMs: start, endMs: end)!
        XCTAssertTrue(cut.hasPrefix("Recording was cut short around "))
        XCTAssertEqual(
            DayStripAccessibility.gapCauseLabel(.stillRecording, startMs: start, endMs: end),
            "Still recording"
        )
        let unverified = DayStripAccessibility.gapCauseLabel(.cantVerify, startMs: start, endMs: end)!
        XCTAssertTrue(unverified.hasPrefix("Can't verify what happened here, "))
        XCTAssertNil(DayStripAccessibility.gapCauseLabel(.none, startMs: start, endMs: end))
    }

    /// R6: a purged span names the rule when identity is on the wire (app name
    /// preferred, then root domain, then bundle id) and degrades to the generic
    /// privacy-rule sentence when identity-free — never inventing a name.
    func testPurgedLabelNamesRuleAndDegradesIdentityFree() {
        let start = dayStart, end = dayStart + hour
        let named = DayStripAccessibility.purgedLabel(
            DayPurgedInterval(startMs: start, endMs: end, bundleId: "com.hnc.Discord", appName: "Discord")
        )
        XCTAssertTrue(named.hasPrefix("Removed by your 'disable Discord' rule, "))
        let domain = DayStripAccessibility.purgedLabel(
            DayPurgedInterval(startMs: start, endMs: end, rootDomain: "discord.com")
        )
        XCTAssertTrue(domain.hasPrefix("Removed by your 'disable discord.com' rule, "))
        let bundleOnly = DayStripAccessibility.purgedLabel(
            DayPurgedInterval(startMs: start, endMs: end, bundleId: "com.hnc.Discord")
        )
        XCTAssertTrue(bundleOnly.hasPrefix("Removed by your 'disable com.hnc.Discord' rule, "))
        let bare = DayStripAccessibility.purgedLabel(DayPurgedInterval(startMs: start, endMs: end))
        XCTAssertTrue(bare.hasPrefix("Removed by a privacy rule, "))
    }

    /// The purged-band mapping is a straight union across the day's recordings
    /// (identity rides along for the R6 copy).
    func testPurgedBandsUnionAcrossRecordings() {
        let spans = [
            DaySegmentRecording(
                name: "rec-a", startMs: dayStart, endMs: dayStart + hour,
                purged: [DayPurgedInterval(startMs: dayStart + 100, endMs: dayStart + 200, appName: "Discord")]
            ),
            DaySegmentRecording(name: "rec-b", startMs: dayStart + 2 * hour, endMs: dayStart + 3 * hour),
        ]
        XCTAssertEqual(
            DayPurgedInterval.bands(from: spans),
            [DayPurgedInterval(startMs: dayStart + 100, endMs: dayStart + 200, appName: "Discord")]
        )
    }

    // MARK: - Accessibility labels

    func testAccessibilityLabelsNameSegmentBlockedAndPlayhead() {
        let segment = DayStripSegment(
            recording: "rec-a", taskIndex: 0, name: "Payroll walkthrough", category: "finance",
            startMs: dayStart, endMs: dayStart + hour
        )
        XCTAssertTrue(DayStripAccessibility.segmentLabel(segment).hasPrefix("Payroll walkthrough, task, "))
        let base = DayStripBaseTrack(
            recording: "rec-a", title: "Payroll walkthrough",
            startMs: dayStart, endMs: dayStart + hour
        )
        XCTAssertTrue(DayStripAccessibility.baseTrackLabel(base).contains("unsplit, still searchable"))
        let band = DayStripBlockedBand(startMs: dayStart, endMs: dayStart + hour)
        XCTAssertTrue(DayStripAccessibility.blockedLabel(band).hasPrefix("Blocked at capture, "))
        XCTAssertTrue(DayStripAccessibility.playheadLabel(ms: dayStart).hasPrefix("Playhead at "))
    }

    /// AE10: no purged surface pairs "blocked" (or the blocked entry's
    /// "nothing captured" claim) with a purge — every purged legend and
    /// accessibility string avoids both words, so a purge never reads as a
    /// capture-time block.
    func testPurgedStringsNeverSayBlockedOrNothingCaptured() {
        let purgedLegendText = DayStripLegend.items.first { $0.swatch == .purged }?.text
        XCTAssertNotNil(purgedLegendText, "the legend carries a purged entry")
        let purgedStrings = [
            DayStripAccessibility.purgedLabel(
                DayPurgedInterval(startMs: dayStart, endMs: dayStart + hour, appName: "Discord")
            ),
            DayStripAccessibility.purgedLabel(
                DayPurgedInterval(startMs: dayStart, endMs: dayStart + hour)
            ),
            purgedLegendText ?? "",
        ]
        for text in purgedStrings {
            XCTAssertFalse(text.lowercased().contains("blocked"), "purged string says 'blocked': \(text)")
            XCTAssertFalse(
                text.lowercased().contains("nothing captured"),
                "purged string says 'nothing captured': \(text)"
            )
        }
    }
}
