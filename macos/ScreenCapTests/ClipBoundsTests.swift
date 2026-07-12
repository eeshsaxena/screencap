import XCTest
@testable import ScreenCap

/// SCR-219 (U5) — pins the pure clip-bounds contract: snap-to-moment
/// resolution + overlap tie-break, the centered fixed-window fallback, edge
/// clamping, bidirectional drag adjust, the long-clip threshold, and the
/// `isClippable` gate. The math is load-bearing (R1/R2/R3/R8) and the whole
/// point of extracting `ClipBoundsResolver` as a pure value enum is that it is
/// exercisable here without SwiftUI, AVFoundation, or a daemon — the same
/// discipline as `DayPlaybackEngineTests` over `DayMediaMap`.
final class ClipBoundsTests: XCTestCase {

    // Unix-second task times, chosen so second→ms conversion is exact.
    private func task(
        index: Int, startSec: Double, endSec: Double, name: String = "task"
    ) -> RecordingTask {
        RecordingTask(taskIndex: index, startTs: startSec, endTs: endSec, name: name)
    }

    // MARK: - Snap to containing task (R1)

    func testPlayheadInsideTaskSnapsToItsBounds() {
        // task [1000s, 1100s) → [1_000_000ms, 1_100_000ms); playhead 1_050_000ms.
        let tasks = [task(index: 0, startSec: 1000, endSec: 1100)]
        let containing = ClipBoundsResolver.containingTask(tasks: tasks, playheadMs: 1_050_000)
        XCTAssertEqual(containing?.taskIndex, 0)

        let bounds = ClipBoundsResolver.momentBounds(
            task: containing!, footageStartMs: 0, footageEndMs: 5_000_000
        )
        XCTAssertEqual(bounds, ClipRange(startMs: 1_000_000, endMs: 1_100_000))
    }

    func testPlayheadOutsideEveryTaskResolvesToNoTask() {
        let tasks = [task(index: 0, startSec: 1000, endSec: 1100)]
        // Half-open: the exact end is NOT contained.
        XCTAssertNil(ClipBoundsResolver.containingTask(tasks: tasks, playheadMs: 1_100_000))
        XCTAssertNil(ClipBoundsResolver.containingTask(tasks: tasks, playheadMs: 2_000_000))
    }

    func testTaskEndIsExclusiveAndStartIsInclusive() {
        let tasks = [task(index: 0, startSec: 1000, endSec: 1100)]
        XCTAssertEqual(
            ClipBoundsResolver.containingTask(tasks: tasks, playheadMs: 1_000_000)?.taskIndex, 0,
            "start is inclusive"
        )
        XCTAssertNil(
            ClipBoundsResolver.containingTask(tasks: tasks, playheadMs: 1_100_000),
            "end is exclusive"
        )
    }

    // MARK: - Overlap tie-break (KD2: innermost / most-recently-started)

    func testOverlappingTasksPickInnermostMostRecentDeterministically() {
        let outer = task(index: 0, startSec: 1000, endSec: 1100) // [1_000_000, 1_100_000)
        let inner = task(index: 1, startSec: 1040, endSec: 1060) // [1_040_000, 1_060_000)
        let playhead = 1_050_000

        // Both contain the playhead; the inner (later start, tighter) wins,
        // and the result must not depend on array order.
        let a = ClipBoundsResolver.containingTask(tasks: [outer, inner], playheadMs: playhead)
        let b = ClipBoundsResolver.containingTask(tasks: [inner, outer], playheadMs: playhead)
        XCTAssertEqual(a?.taskIndex, 1)
        XCTAssertEqual(b?.taskIndex, 1)
    }

    func testIdenticalIntervalsTieBreakOnTaskIndexDeterministically() {
        // Same start + end → deterministic by highest task_index (unique per
        // recording), independent of input order.
        let first = task(index: 3, startSec: 1000, endSec: 1100)
        let second = task(index: 7, startSec: 1000, endSec: 1100)
        let playhead = 1_050_000
        XCTAssertEqual(
            ClipBoundsResolver.containingTask(tasks: [first, second], playheadMs: playhead)?.taskIndex, 7
        )
        XCTAssertEqual(
            ClipBoundsResolver.containingTask(tasks: [second, first], playheadMs: playhead)?.taskIndex, 7
        )
    }

    // MARK: - Fixed-window fallback (R3 / AE1)

    func testEmptyTasksFallBackToCenteredFixedWindow() {
        XCTAssertNil(ClipBoundsResolver.containingTask(tasks: [], playheadMs: 1_050_000))

        let window = ClipBoundsResolver.fixedWindow(
            centerMs: 1_050_000, span: .thirtySeconds,
            footageStartMs: 0, footageEndMs: 10_000_000
        )
        // 30s total, centered → ±15s, never whole-recording or zero-length.
        XCTAssertEqual(window, ClipRange(startMs: 1_035_000, endMs: 1_065_000))
        XCTAssertEqual(window.durationMs, 30_000)
    }

    func testFixedWindowSpansAreTotalDurationsCenteredOnTimestamp() {
        let center = 1_000_000
        let footageEnd = 10_000_000
        let cases: [(ClipSpan, Int)] = [
            (.fifteenSeconds, 15_000),
            (.thirtySeconds, 30_000),
            (.twoMinutes, 120_000),
        ]
        for (span, total) in cases {
            let w = ClipBoundsResolver.fixedWindow(
                centerMs: center, span: span, footageStartMs: 0, footageEndMs: footageEnd
            )
            XCTAssertEqual(w.durationMs, total, "\(span) is a \(total)ms TOTAL window")
            XCTAssertEqual(w.startMs, center - total / 2)
            XCTAssertEqual(w.endMs, center + total / 2)
        }
    }

    // MARK: - Edge clamp (R3)

    func testFixedWindowClampsToFootageAtLeadingEdge() {
        // Center 10s into footage, 30s window → start would precede footage.
        let footageStart = 1_040_000
        let w = ClipBoundsResolver.fixedWindow(
            centerMs: 1_050_000, span: .thirtySeconds,
            footageStartMs: footageStart, footageEndMs: 10_000_000
        )
        XCTAssertEqual(w.startMs, footageStart, "start clamps to footage edge")
        XCTAssertEqual(w.endMs, 1_065_000, "center + half is unchanged")
        XCTAssertEqual(w.durationMs, 25_000, "duration reflects the clamp, not the full span")
    }

    func testFixedWindowClampsToFootageAtTrailingEdge() {
        let footageEnd = 1_060_000
        let w = ClipBoundsResolver.fixedWindow(
            centerMs: 1_050_000, span: .thirtySeconds,
            footageStartMs: 0, footageEndMs: footageEnd
        )
        XCTAssertEqual(w.startMs, 1_035_000)
        XCTAssertEqual(w.endMs, footageEnd, "end clamps to footage edge")
        XCTAssertEqual(w.durationMs, 25_000)
    }

    func testMomentBoundsClampToFootage() {
        // A labeled moment that starts before / ends after the available footage.
        let moment = task(index: 0, startSec: 900, endSec: 2100) // [900_000, 2_100_000)
        let bounds = ClipBoundsResolver.momentBounds(
            task: moment, footageStartMs: 1_000_000, footageEndMs: 2_000_000
        )
        XCTAssertEqual(bounds, ClipRange(startMs: 1_000_000, endMs: 2_000_000))
    }

    // MARK: - Drag adjust (R2: tighten OR extend, clamped)

    func testExtendStartClampsToFootage() {
        let range = ClipRange(startMs: 1_035_000, endMs: 1_065_000)
        let extended = ClipBoundsResolver.adjustStart(
            range, toMs: 900_000, footageStartMs: 1_000_000, footageEndMs: 2_000_000
        )
        XCTAssertEqual(extended.startMs, 1_000_000, "start extends only as far as footage")
        XCTAssertEqual(extended.endMs, 1_065_000)
    }

    func testExtendEndClampsToFootage() {
        let range = ClipRange(startMs: 1_035_000, endMs: 1_065_000)
        let extended = ClipBoundsResolver.adjustEnd(
            range, toMs: 3_000_000, footageStartMs: 1_000_000, footageEndMs: 2_000_000
        )
        XCTAssertEqual(extended.startMs, 1_035_000)
        XCTAssertEqual(extended.endMs, 2_000_000, "end extends only as far as footage")
    }

    func testTightenBothHandlesInward() {
        let range = ClipRange(startMs: 1_035_000, endMs: 1_065_000)
        let footage = (start: 1_000_000, end: 2_000_000)
        let tightStart = ClipBoundsResolver.adjustStart(
            range, toMs: 1_050_000, footageStartMs: footage.start, footageEndMs: footage.end
        )
        XCTAssertEqual(tightStart.startMs, 1_050_000)
        let tightEnd = ClipBoundsResolver.adjustEnd(
            tightStart, toMs: 1_055_000, footageStartMs: footage.start, footageEndMs: footage.end
        )
        XCTAssertEqual(tightEnd, ClipRange(startMs: 1_050_000, endMs: 1_055_000))
    }

    func testDragCannotCollapseBelowMinimumDuration() {
        let range = ClipRange(startMs: 1_035_000, endMs: 1_065_000)
        let footage = (start: 0, end: 10_000_000)
        // Dragging start past the end is capped at end - minDuration.
        let clampedStart = ClipBoundsResolver.adjustStart(
            range, toMs: 1_090_000, footageStartMs: footage.start, footageEndMs: footage.end
        )
        XCTAssertEqual(clampedStart.startMs, 1_065_000 - ClipBoundsResolver.minDurationMs)
        // Dragging end past the start is capped at start + minDuration.
        let clampedEnd = ClipBoundsResolver.adjustEnd(
            range, toMs: 1_000_000, footageStartMs: footage.start, footageEndMs: footage.end
        )
        XCTAssertEqual(clampedEnd.endMs, 1_035_000 + ClipBoundsResolver.minDurationMs)
    }

    // MARK: - Long-clip nudge threshold (R2)

    func testLongClipThresholdIsStrictlyOverTwoMinutes() {
        XCTAssertFalse(ClipBoundsResolver.isLongClip(ClipRange(startMs: 0, endMs: 120_000)),
                       "exactly 2 minutes is not a long clip")
        XCTAssertTrue(ClipBoundsResolver.isLongClip(ClipRange(startMs: 0, endMs: 120_001)),
                      "over 2 minutes triggers the nudge")
    }

    func testFreshTwoMinuteFixedWindowDoesNotTriggerNudge() {
        let w = ClipBoundsResolver.fixedWindow(
            centerMs: 5_000_000, span: .twoMinutes, footageStartMs: 0, footageEndMs: 10_000_000
        )
        XCTAssertEqual(w.durationMs, 120_000)
        XCTAssertFalse(ClipBoundsResolver.isLongClip(w),
                       "the largest fixed-window preset is not itself 'long'")
    }

    // MARK: - Duration readout

    func testDurationLabelFormatsMinutesAndSeconds() {
        XCTAssertEqual(ClipBoundsResolver.durationLabel(ms: 30_000), "0:30")
        XCTAssertEqual(ClipBoundsResolver.durationLabel(ms: 90_000), "1:30")
        XCTAssertEqual(ClipBoundsResolver.durationLabel(ms: 120_000), "2:00")
        XCTAssertEqual(ClipBoundsResolver.durationLabel(ms: 0), "0:00")
    }

    // MARK: - isClippable gate (R8)

    func testStubRecordingIsNotClippable() {
        XCTAssertFalse(makeSummary(uploaded: true, isStub: true, durationSeconds: 42).isClippable)
        XCTAssertFalse(makeSummary(uploaded: false, isStub: true, durationSeconds: 42).isClippable)
    }

    func testAlreadyUploadedButLocalRecordingIsStillClippable() {
        // The load-bearing difference from isUploadEligible: an uploaded (but
        // not evicted) recording is clippable, though it is not upload-eligible.
        let rec = makeSummary(uploaded: true, isStub: false, durationSeconds: 42)
        XCTAssertTrue(rec.isClippable)
        XCTAssertFalse(rec.isUploadEligible)
    }

    func testZeroOrMissingDurationIsNotClippable() {
        XCTAssertFalse(makeSummary(uploaded: false, isStub: false, durationSeconds: 0).isClippable)
        XCTAssertFalse(makeSummary(uploaded: false, isStub: false, durationSeconds: nil).isClippable)
    }

    func testLocalNonStubWithFootageIsClippable() {
        XCTAssertTrue(makeSummary(uploaded: false, isStub: false, durationSeconds: 42).isClippable)
    }

    // MARK: - Helpers

    /// Build a `RecordingSummary` via its real Decodable init so the test rides
    /// the same path the `screencap list --json` consumer takes.
    private func makeSummary(
        uploaded: Bool,
        isStub: Bool,
        durationSeconds: Double?
    ) -> RecordingSummary {
        let durationJSON = durationSeconds.map { String($0) } ?? "null"
        let json = """
        {
          "name": "rec-clip-test",
          "date": "2026-07-12",
          "duration": "0:42",
          "size_mb": "1.2",
          "has_audio": false,
          "transcribed": false,
          "uploaded": \(uploaded),
          "is_stub": \(isStub),
          "chunks_total": 0,
          "chunks_uploaded": 0,
          "intent": null,
          "started_at": 1752300000.0,
          "duration_seconds": \(durationJSON),
          "drops": null
        }
        """
        return try! JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }
}
