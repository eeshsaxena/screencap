import XCTest
@testable import Screencap

/// Moments — the merged surface's pure model (`MomentsModel`): the union of
/// app-detected task spans and user-clipped ranges into one footage-time
/// interleaved, day-grouped list (KTD-3), the type + substring filter, and the
/// resolved zero/filter state (R7/KTD-6, R21). Rendering (MomentsView, thumbnails,
/// row actions) is verified by build-and-run.
final class MomentsModelTests: XCTestCase {

    // MARK: - Fixtures

    private func task(
        recording: String = "rec-1",
        taskIndex: Int = 0,
        name: String = "Task",
        category: String? = nil,
        startTs: Double,
        endTs: Double? = nil
    ) -> TasksQueryTask {
        TasksQueryTask(
            recording: recording,
            taskIndex: taskIndex,
            startTs: startTs,
            endTs: endTs ?? startTs + 60,
            name: name,
            category: category
        )
    }

    private func clip(
        id: String,
        sourceDay: String,
        startMs: Int,
        endMs: Int? = nil,
        createdAt: Double = 100,
        creator: String = "ui"
    ) -> ClipRecord {
        ClipRecord(
            id: id,
            sourceRecording: "rec-1",
            sourceDay: sourceDay,
            startMs: startMs,
            endMs: endMs ?? startMs + 30_000,
            createdAt: createdAt,
            creator: creator,
            honestyFlags: ClipHonestyFlags(videoCaptureBlockedOnly: true, policyPurgedPartial: false),
            path: "/tmp/\(id).mp4"
        )
    }

    // MARK: - Merge: footage-time interleave + unit conversion (R2, KTD-3)

    func testMergeInterleavesByFootageTimeAcrossUnits() {
        // A task's Unix-seconds start (1000s → 1_000_000ms) must land adjacent to a
        // clip at the same absolute ms — proving the seconds↔milliseconds conversion.
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [
                task(taskIndex: 0, name: "Morning", startTs: 500),   // 500_000 ms
                task(taskIndex: 1, name: "Noon", startTs: 1000),     // 1_000_000 ms
            ])],
            clips: [
                clip(id: "cSame", sourceDay: "2026-07-17", startMs: 1_000_000),
                clip(id: "cLate", sourceDay: "2026-07-17", startMs: 2_000_000),
            ]
        )
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(
            groups[0].rows.map(\.id),
            ["auto:rec-1#0", "auto:rec-1#1", "clip:cSame", "clip:cLate"]
        )
    }

    func testMergeDaysReverseChronological() {
        let groups = MomentsModel.merge(
            days: [
                TasksQueryDay(date: "2026-07-16", tasks: [task(startTs: 1000)]),
                TasksQueryDay(date: "2026-07-17", tasks: [task(startTs: 1000)]),
            ],
            clips: []
        )
        XCTAssertEqual(groups.map(\.dayKey), ["2026-07-17", "2026-07-16"])
    }

    // MARK: - Deep-past = only kept moments (R6)

    func testClipOnlyDayInDeepPastHoldsOnlyClippedRow() {
        // A recent day has tasks; an older day (its task segments evicted) has only a
        // clip. The older group holds just the Clipped row — the durable moment
        // outlives its day.
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [task(startTs: 1000)])],
            clips: [clip(id: "old", sourceDay: "2026-05-01", startMs: 500_000)]
        )
        XCTAssertEqual(groups.map(\.dayKey), ["2026-07-17", "2026-05-01"])
        XCTAssertEqual(groups[1].rows.map(\.isClipped), [true])
        XCTAssertEqual(groups[1].rows.map(\.id), ["clip:old"])
    }

    // MARK: - Row marker (R3)

    func testIsClippedMarker() {
        let auto = MomentsModel.MomentRow.auto(
            TasksModel.TaskRow(recording: "r", recordingId: nil, taskIndex: 0,
                               name: "n", category: nil, startTs: 1, endTs: 2, day: Date())
        )
        let clipped = MomentsModel.MomentRow.clipped(clip(id: "c", sourceDay: "2026-07-17", startMs: 0))
        XCTAssertFalse(auto.isClipped)
        XCTAssertTrue(clipped.isClipped)
    }

    // MARK: - Deterministic tie-break at equal footage time

    func testSameFootageTimeOrdersAutoBeforeClippedDeterministically() {
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [task(taskIndex: 0, startTs: 1000)])], // 1_000_000
            clips: [clip(id: "tie", sourceDay: "2026-07-17", startMs: 1_000_000)]
        )
        XCTAssertEqual(groups[0].rows.map(\.id), ["auto:rec-1#0", "clip:tie"])
    }

    func testMergeEmptyInputsYieldsNoGroups() {
        XCTAssertTrue(MomentsModel.merge(days: [], clips: []).isEmpty)
    }

    // MARK: - Filter (R5)

    func testClippedFilterIsolatesClips() {
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [
                task(taskIndex: 0, name: "A", startTs: 500),
                task(taskIndex: 1, name: "B", startTs: 1500),
            ])],
            clips: [clip(id: "k", sourceDay: "2026-07-17", startMs: 1_000_000)]
        )
        let clippedOnly = MomentsModel.filter(groups, query: "", clippedOnly: true)
        XCTAssertEqual(clippedOnly.flatMap { $0.rows }.map(\.id), ["clip:k"])
    }

    func testClippedFilterSurfacesClipOlderThanTaskWindow() {
        // All clips are loaded (KTD-2), so a clip whose day predates any loaded task
        // still appears under the Clipped filter — no paging dependency in the model.
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [task(startTs: 1000)])],
            clips: [clip(id: "ancient", sourceDay: "2025-01-01", startMs: 10_000)]
        )
        let clippedOnly = MomentsModel.filter(groups, query: "", clippedOnly: true)
        XCTAssertEqual(clippedOnly.flatMap { $0.rows }.map(\.id), ["clip:ancient"])
    }

    func testSubstringFilterNarrowsAutoRowsAndExcludesClips() {
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [
                task(taskIndex: 0, name: "Salesforce triage", startTs: 500),
                task(taskIndex: 1, name: "Looker digging", startTs: 1500),
            ])],
            clips: [clip(id: "k", sourceDay: "2026-07-17", startMs: 1_000_000)]
        )
        let filtered = MomentsModel.filter(groups, query: "sales", clippedOnly: false)
        XCTAssertEqual(filtered.flatMap { $0.rows }.map(\.id), ["auto:rec-1#0"])
    }

    func testEmptyQueryLeavesGroupsUnchanged() {
        let groups = MomentsModel.merge(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [task(startTs: 1000)])],
            clips: [clip(id: "k", sourceDay: "2026-07-17", startMs: 2_000_000)]
        )
        XCTAssertEqual(MomentsModel.filter(groups, query: "  ", clippedOnly: false), groups)
    }

    // MARK: - Resolved list state (R7/KTD-6, R21)

    func testClipsOverrideSystemZero() {
        // The crucial R7/KTD-6 guarantee: with a clip present but NO tasks and NO
        // recordings, the list is populated — never the enable-Intelligence zero.
        let state = MomentsModel.listState(
            days: [],
            clips: [clip(id: "k", sourceDay: "2026-07-17", startMs: 1_000_000)],
            recordings: [],
            verdict: nil
        )
        guard case .populated(let groups) = state else {
            return XCTFail("expected populated, got \(state)")
        }
        XCTAssertEqual(groups.flatMap { $0.rows }.map(\.id), ["clip:k"])
    }

    func testEmptyEverythingFallsToSystemZero() {
        let state = MomentsModel.listState(days: [], clips: [], recordings: [], verdict: nil)
        guard case .systemZero(let zero) = state else {
            return XCTFail("expected systemZero, got \(state)")
        }
        XCTAssertEqual(zero.state, .unknown) // recordings empty → "nothing on file"
    }

    func testClippedFilterWithNoClipsIsFilterZeroNotSystemZero() {
        // Tasks exist but none are clipped → filter-zero, distinct from the
        // system-wide zero (R21), so it never misreads as "you have nothing".
        let state = MomentsModel.listState(
            days: [TasksQueryDay(date: "2026-07-17", tasks: [task(startTs: 1000)])],
            clips: [],
            recordings: [TasksQueryRecordingStatus(name: "rec-1")],
            clippedOnly: true,
            verdict: nil
        )
        guard case .filterZero(_, let clippedOnly) = state else {
            return XCTFail("expected filterZero, got \(state)")
        }
        XCTAssertTrue(clippedOnly)
    }
}
