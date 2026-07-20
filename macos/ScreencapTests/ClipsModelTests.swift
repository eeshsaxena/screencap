import XCTest
@testable import Screencap

/// U11 — the Clips surface's pure model (`ClipsModel`): source-day grouping,
/// day-label + range/duration formatting, the honesty-flag readout, the
/// cross-recording split-range guard (KTD-8), and the honest failure copy.
/// Rendering (ClipsView, thumbnails, playback, share) is verified by build-and-run.
final class ClipsModelTests: XCTestCase {

    // MARK: - Fixtures

    private func clip(
        id: String = "c1",
        sourceRecording: String = "rec-1",
        sourceDay: String,
        startMs: Int = 1_000_000,
        endMs: Int = 1_030_000,
        createdAt: Double = 100,
        creator: String = "ui",
        policyPurgedPartial: Bool = false
    ) -> ClipRecord {
        ClipRecord(
            id: id,
            sourceRecording: sourceRecording,
            sourceDay: sourceDay,
            startMs: startMs,
            endMs: endMs,
            createdAt: createdAt,
            creator: creator,
            honestyFlags: ClipHonestyFlags(
                videoCaptureBlockedOnly: true, policyPurgedPartial: policyPurgedPartial
            ),
            path: "/tmp/\(id).mp4"
        )
    }

    // MARK: - Grouping

    func testGroupsByDayNewestFirst() {
        let groups = ClipsModel.groups([
            clip(id: "a", sourceDay: "2026-07-16"),
            clip(id: "b", sourceDay: "2026-07-17"),
            clip(id: "c", sourceDay: "2026-07-16"),
        ])
        XCTAssertEqual(groups.map(\.sourceDay), ["2026-07-17", "2026-07-16"])
        XCTAssertEqual(groups[0].clips.map(\.id), ["b"])
        XCTAssertEqual(Set(groups[1].clips.map(\.id)), ["a", "c"])
    }

    func testGroupsNewestClipFirstWithinDay() {
        let groups = ClipsModel.groups([
            clip(id: "older", sourceDay: "2026-07-17", createdAt: 100),
            clip(id: "newer", sourceDay: "2026-07-17", createdAt: 200),
        ])
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups[0].clips.map(\.id), ["newer", "older"])
    }

    func testEmptyClipsGroupsToEmpty() {
        XCTAssertTrue(ClipsModel.groups([]).isEmpty)
    }

    // MARK: - Day labels

    func testDayLabelTodayAndYesterday() {
        let cal = Calendar.current
        let now = Date()
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = cal
        f.timeZone = cal.timeZone
        f.dateFormat = "yyyy-MM-dd"

        let today = f.string(from: now)
        let yesterday = f.string(from: cal.date(byAdding: .day, value: -1, to: now)!)
        XCTAssertEqual(ClipsModel.dayLabel(today, now: now, calendar: cal), "Today")
        XCTAssertEqual(ClipsModel.dayLabel(yesterday, now: now, calendar: cal), "Yesterday")
    }

    func testDayLabelUnparseableFallsBackToRaw() {
        XCTAssertEqual(ClipsModel.dayLabel("not-a-date"), "not-a-date")
    }

    // MARK: - Range / duration formatting

    func testDurationTextMinutesSeconds() {
        XCTAssertEqual(ClipsModel.durationText(clip(sourceDay: "2026-07-17", startMs: 0, endMs: 30_000)), "0:30")
        XCTAssertEqual(ClipsModel.durationText(clip(sourceDay: "2026-07-17", startMs: 0, endMs: 90_000)), "1:30")
        // Never negative.
        XCTAssertEqual(ClipsModel.durationText(clip(sourceDay: "2026-07-17", startMs: 50, endMs: 40)), "0:00")
    }

    func testRangeClockTextShape() {
        let text = ClipsModel.rangeClockText(clip(sourceDay: "2026-07-17"))
        // "HH:mm–HH:mm" — tz-independent in shape (11 chars, one en-dash).
        XCTAssertEqual(text.count, 11)
        XCTAssertEqual(text.filter { $0 == "–" }.count, 1)
        XCTAssertEqual(text.filter { $0 == ":" }.count, 2)
    }

    // MARK: - Honesty flags

    func testIsAgentCreated() {
        XCTAssertTrue(ClipsModel.isAgentCreated(clip(sourceDay: "2026-07-17", creator: "mcp")))
        XCTAssertFalse(ClipsModel.isAgentCreated(clip(sourceDay: "2026-07-17", creator: "ui")))
    }

    func testSummaryLineCarriesPolicyPurgedFlag() {
        let flagged = clip(sourceDay: "2026-07-17", policyPurgedPartial: true)
        XCTAssertTrue(ClipsModel.summaryLine(flagged).contains(ClipsModel.policyPurgedFlagText))
        let clean = clip(sourceDay: "2026-07-17", policyPurgedPartial: false)
        XCTAssertFalse(ClipsModel.summaryLine(clean).contains(ClipsModel.policyPurgedFlagText))
    }

    func testHonestyFlagsDecodeFromEmptyDict() throws {
        let flags = try JSONDecoder().decode(ClipHonestyFlags.self, from: Data("{}".utf8))
        XCTAssertFalse(flags.videoCaptureBlockedOnly)
        XCTAssertFalse(flags.policyPurgedPartial)
    }

    // MARK: - Cross-recording split-range guard (KTD-8)

    private let trackA = ClipsModel.RecordingTrack(recording: "rec-a", startMs: 0, endMs: 100)
    private let trackB = ClipsModel.RecordingTrack(recording: "rec-b", startMs: 100, endMs: 200)

    func testResolveRangeSingleRecording() {
        XCTAssertEqual(
            ClipsModel.resolveRange(startMs: 10, endMs: 50, tracks: [trackA, trackB]),
            .single(recording: "rec-a")
        )
    }

    func testResolveRangeCrossesRecordingsNamesSplit() {
        XCTAssertEqual(
            ClipsModel.resolveRange(startMs: 50, endMs: 150, tracks: [trackA, trackB]),
            .crossesRecordings(splitMs: 100)
        )
    }

    func testResolveRangeNoFootage() {
        XCTAssertEqual(
            ClipsModel.resolveRange(startMs: 300, endMs: 400, tracks: [trackA, trackB]),
            .noFootage
        )
    }

    func testResolveRangeHalfOpenBoundaryDoesNotSpuriouslyCross() {
        // A range abutting the A/B boundary sits entirely in B (half-open [start,end)).
        XCTAssertEqual(
            ClipsModel.resolveRange(startMs: 100, endMs: 150, tracks: [trackA, trackB]),
            .single(recording: "rec-b")
        )
    }

    // MARK: - Failure copy

    func testCrossRecordingMessageNamesInstant() {
        // Uses the local HH:mm formatter, so assert structure, not an exact clock.
        let msg = ClipsModel.crossRecordingMessage(splitMs: 1_000_000)
        XCTAssertTrue(msg.contains("crosses a recording boundary"))
        XCTAssertTrue(msg.contains("single recording"))
    }

    func testReasonMessagePolicyPurged() {
        XCTAssertTrue(ClipsModel.reasonMessage("policy_purged").contains("privacy rules"))
        XCTAssertTrue(ClipsModel.reasonMessage("clip_busy").contains("Try again"))
        // An unknown reason is surfaced verbatim, never swallowed.
        XCTAssertTrue(ClipsModel.reasonMessage("future_reason").contains("future_reason"))
    }

    func testErrorMessageMapsSealedStore() {
        let locked = ClipsModel.errorMessage(DaemonClientError.envelopeError(code: "store_locked", rawBody: Data()))
        XCTAssertTrue(locked.contains("locked"))
        let absent = ClipsModel.errorMessage(DaemonClientError.envelopeError(code: "store_absent", rawBody: Data()))
        XCTAssertTrue(absent.contains("isn't set up"))
    }
}
