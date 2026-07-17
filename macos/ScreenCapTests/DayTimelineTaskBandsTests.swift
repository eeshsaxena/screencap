import XCTest
@testable import ScreenCap

/// U10 (SCR-214) — the Day-timeline task bands: decoding the additive `tasks`
/// field on `timeline.day`, mapping N tasks → N bands over each recording's
/// "unsplit — still searchable" base track, the honest legend, the
/// nothing-captured gap complement, unique band ids, and the accessibility
/// announcements for every region type. Rendering itself is verified by
/// build-and-run; these pin the pure logic + decoding (the DayStripLayoutTests
/// pattern).
final class DayTimelineTaskBandsTests: XCTestCase {

    private let hour = DayStripLayout.hourMs
    private let dayStart = 1_800_000_000_000

    // MARK: - Decoding the additive `tasks` field

    /// A U9/v2 `timeline.day` recording carries its named task segments inline.
    func testTimelineDayDecodesNestedTasks() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.0.0",
          "api_schema_version": 2, "date": "2026-07-13",
          "recordings": [
            {
              "name": "rec-a", "recording_id": "id-a", "state": "ready",
              "start_ms": 100000, "end_ms": 400000,
              "blocked_proven": [], "unverifiable": [],
              "tasks": [
                {"task_index": 0, "start_ts": 100.0, "end_ts": 250.0,
                 "name": "Payroll run in Gusto", "category": "finance", "confidence": "high"},
                {"task_index": 1, "start_ts": 250.0, "end_ts": 400.0, "name": "Email triage",
                 "category": null, "confidence": null}
              ]
            }
          ]
        }
        """
        let resp = try JSONDecoder().decode(TimelineDayResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.recordings.count, 1)
        let rec = resp.recordings[0]
        XCTAssertEqual(rec.tasks.count, 2)
        XCTAssertEqual(rec.tasks[0].taskIndex, 0)
        XCTAssertEqual(rec.tasks[0].name, "Payroll run in Gusto")
        XCTAssertEqual(rec.tasks[0].category, "finance")
        XCTAssertEqual(rec.tasks[0].startTs, 100.0)
        XCTAssertNil(rec.tasks[1].category, "heuristic-style task carries no category")
    }

    /// An older daemon (pre-U9 `timeline.day`) omits `tasks` entirely — it must
    /// decode to an empty list, never fail.
    func testTimelineDayDecodesWithoutTasksFieldFromOlderDaemon() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.0.0",
          "api_schema_version": 1, "date": "2026-07-13",
          "recordings": [
            {
              "name": "rec-legacy", "recording_id": null, "state": "ready",
              "start_ms": 100000, "end_ms": 200000,
              "blocked_proven": [], "unverifiable": []
            }
          ]
        }
        """
        let resp = try JSONDecoder().decode(TimelineDayResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.recordings.count, 1)
        XCTAssertTrue(resp.recordings[0].tasks.isEmpty, "absent `tasks` → empty, not an error")
    }

    // MARK: - Decoding the additive provenance fields (v3)

    /// A v3 `timeline.day` recording carries `end_status` + `purged` spans, and
    /// the envelope carries `store_mounted`.
    func testTimelineDayDecodesProvenanceFields() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.0.0",
          "api_schema_version": 3, "date": "2026-07-13",
          "store_mounted": false,
          "recordings": [
            {
              "name": "rec-a", "recording_id": "id-a", "state": "ready",
              "start_ms": 100000, "end_ms": 400000,
              "blocked_proven": [], "unverifiable": [],
              "end_status": "interrupted",
              "purged": [
                {"start_ms": 120000, "end_ms": 150000,
                 "bundle_id": "com.1password.1password", "app_name": "1Password",
                 "root_domain": null},
                {"start_ms": 200000, "end_ms": 210000,
                 "bundle_id": null, "app_name": null, "root_domain": "chase.com"}
              ]
            }
          ]
        }
        """
        let resp = try JSONDecoder().decode(TimelineDayResponse.self, from: Data(json.utf8))
        XCTAssertFalse(resp.storeMounted)
        let rec = resp.recordings[0]
        XCTAssertEqual(rec.endStatus, "interrupted")
        XCTAssertEqual(rec.purged.count, 2)
        XCTAssertEqual(rec.purged[0].startMs, 120_000)
        XCTAssertEqual(rec.purged[0].endMs, 150_000)
        XCTAssertEqual(rec.purged[0].bundleId, "com.1password.1password")
        XCTAssertEqual(rec.purged[0].appName, "1Password")
        XCTAssertNil(rec.purged[0].rootDomain)
        XCTAssertNil(rec.purged[1].bundleId)
        XCTAssertEqual(rec.purged[1].rootDomain, "chase.com")
    }

    /// An older daemon (pre-v3 `timeline.day`) omits `end_status`, `purged`,
    /// and `store_mounted` entirely — absence is unknown provenance, never a
    /// decode error: nil endStatus, empty purged, storeMounted true.
    func testTimelineDayDecodesWithoutProvenanceFieldsFromOlderDaemon() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.0.0",
          "api_schema_version": 2, "date": "2026-07-13",
          "recordings": [
            {
              "name": "rec-legacy", "recording_id": null, "state": "ready",
              "start_ms": 100000, "end_ms": 200000,
              "blocked_proven": [], "unverifiable": []
            }
          ]
        }
        """
        let resp = try JSONDecoder().decode(TimelineDayResponse.self, from: Data(json.utf8))
        XCTAssertTrue(resp.storeMounted, "absent `store_mounted` → true, not an error")
        let rec = resp.recordings[0]
        XCTAssertNil(rec.endStatus, "absent `end_status` → nil (unknown), not an error")
        XCTAssertTrue(rec.purged.isEmpty, "absent `purged` → empty, not an error")
    }

    /// A purged span whose identity keys are all explicit JSON null decodes
    /// identity-free rather than failing.
    func testPurgedIntervalWithNullIdentityKeysDecodesIdentityFree() throws {
        let json = """
        {"start_ms": 1000, "end_ms": 2000,
         "bundle_id": null, "app_name": null, "root_domain": null}
        """
        let span = try JSONDecoder().decode(DayPurgedInterval.self, from: Data(json.utf8))
        XCTAssertEqual(span.startMs, 1000)
        XCTAssertEqual(span.endMs, 2000)
        XCTAssertNil(span.bundleId)
        XCTAssertNil(span.appName)
        XCTAssertNil(span.rootDomain)
    }

    // MARK: - N tasks → N bands over the base track

    /// Each recording contributes one band per task, with `start_ts`/`end_ts`
    /// (Unix seconds) scaled into the strip's ms coordinate space.
    func testNTasksProduceNBandsWithSecondsScaledToMs() {
        let rec = DaySegmentRecording(
            name: "rec-a", startMs: 100_000, endMs: 400_000,
            tasks: [
                RecordingTask(taskIndex: 0, startTs: 100, endTs: 250, name: "Payroll"),
                RecordingTask(taskIndex: 1, startTs: 250, endTs: 400, name: "Email triage"),
                RecordingTask(taskIndex: 2, startTs: 400, endTs: 400, name: "Zero-width"),
            ]
        )
        let bands = DayStripSegment.bands(from: [rec])
        XCTAssertEqual(bands.count, 3, "N tasks → N bands")
        XCTAssertEqual(bands.map(\.name), ["Payroll", "Email triage", "Zero-width"])
        XCTAssertEqual(bands[0].startMs, 100_000, "seconds → ms")
        XCTAssertEqual(bands[0].endMs, 250_000)
        XCTAssertEqual(bands[1].startMs, 250_000, "adjacent tasks partition the span with no overlap")
    }

    /// A recording with no tasks yields no bands — it renders as a plain unsplit
    /// base band, and the base track is still constructible (no crash).
    func testNoTasksProducesNoBandsButBaseTrackRemains() {
        let rec = DaySegmentRecording(name: "rec-empty", startMs: 100_000, endMs: 200_000)
        XCTAssertTrue(DayStripSegment.bands(from: [rec]).isEmpty)
        let base = DayStripBaseTrack(
            recording: rec.name, title: rec.name, startMs: rec.startMs, endMs: rec.endMs
        )
        XCTAssertEqual(base.startMs, 100_000)
        XCTAssertEqual(base.endMs, 200_000)
    }

    /// `task_index` is only unique *within* a recording, so two recordings can
    /// each hold index 0/1 — the band id is recording-qualified so the strip's
    /// `Identifiable`/`ForEach` never collides across the day.
    func testBandIdsAreUniqueAcrossRecordingsWithSharedTaskIndices() {
        let recs = [
            DaySegmentRecording(
                name: "rec-a", startMs: 0, endMs: 300_000,
                tasks: [
                    RecordingTask(taskIndex: 0, startTs: 0, endTs: 100, name: "A0"),
                    RecordingTask(taskIndex: 1, startTs: 100, endTs: 300, name: "A1"),
                ]
            ),
            DaySegmentRecording(
                name: "rec-b", startMs: 300_000, endMs: 600_000,
                tasks: [
                    RecordingTask(taskIndex: 0, startTs: 300, endTs: 400, name: "B0"),
                    RecordingTask(taskIndex: 1, startTs: 400, endTs: 600, name: "B1"),
                ]
            ),
        ]
        let bands = DayStripSegment.bands(from: recs)
        XCTAssertEqual(bands.count, 4)
        XCTAssertEqual(Set(bands.map(\.id)).count, 4, "recording#index ids never collide")
        XCTAssertEqual(bands[0].id, "rec-a#0")
        XCTAssertEqual(bands[2].id, "rec-b#0")
    }

    // MARK: - Legend

    /// U6 (R8/R10): every legend entry speaks plain language a first-time user
    /// can parse; the empty-stretch entry signals that hover explains the
    /// cause; and the purged entry carries its OWN swatch — pinned unequal to
    /// the blocked one so purged can never render as blocked.
    func testLegendUsesPlainLanguageAndCarriesDistinctPurgedSwatch() {
        let items = DayStripLegend.items
        XCTAssertTrue(items.contains { $0.swatch == .task && $0.text == "task" })
        XCTAssertTrue(items.contains { $0.swatch == .unsplit && $0.text == "recorded — searchable" })
        XCTAssertTrue(items.contains {
            $0.swatch == .nothingCaptured && $0.text == "empty — hover for why"
        })
        XCTAssertTrue(items.contains { $0.swatch == .searchMatch && $0.text == "search match" })
        XCTAssertTrue(items.contains { $0.swatch == .blocked && $0.text == "blocked at capture" })
        let purged = items.first { $0.text == "removed by your rules" }
        XCTAssertEqual(purged?.swatch, DayStripLegend.Swatch.purged, "purged has its own swatch")
        XCTAssertNotEqual(
            purged?.swatch, DayStripLegend.Swatch.blocked,
            "R10: the purged swatch is never the blocked swatch"
        )
        XCTAssertFalse(items.contains { $0.text == "recording" }, "the 'recording' placeholder is retired")
    }

    // MARK: - Nothing-captured gap complement

    /// The gaps are the axis span minus the union of the base tracks — the
    /// between-recording stretches announced as "nothing captured".
    func testNothingCapturedGapsAreTheComplementOfTheBaseTracks() {
        let bounds = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 8 * hour)
        let gaps = DayStripLayout.gaps(bounds: bounds, spans: [
            (startMs: dayStart + hour, endMs: dayStart + 2 * hour),
            (startMs: dayStart + 4 * hour, endMs: dayStart + 5 * hour),
        ])
        XCTAssertEqual(gaps.count, 3)
        XCTAssertEqual(gaps[0].startMs, dayStart)
        XCTAssertEqual(gaps[0].endMs, dayStart + hour)
        XCTAssertEqual(gaps[1].startMs, dayStart + 2 * hour)
        XCTAssertEqual(gaps[1].endMs, dayStart + 4 * hour)
        XCTAssertEqual(gaps[2].endMs, dayStart + 8 * hour, "trailing gap runs to the axis end")
    }

    /// Overlapping / unsorted base spans are merged before the complement, and a
    /// fully-covered axis yields no gaps.
    func testGapsMergeOverlapsAndHandleFullCoverage() {
        let bounds = DayStripLayout.Bounds(startMs: dayStart, endMs: dayStart + 8 * hour)
        let merged = DayStripLayout.gaps(bounds: bounds, spans: [
            (startMs: dayStart + 3 * hour, endMs: dayStart + 5 * hour),
            (startMs: dayStart + hour, endMs: dayStart + 4 * hour),
        ])
        XCTAssertEqual(merged.map(\.startMs), [dayStart, dayStart + 5 * hour])
        let full = DayStripLayout.gaps(bounds: bounds, spans: [
            (startMs: dayStart - hour, endMs: dayStart + 9 * hour),
        ])
        XCTAssertTrue(full.isEmpty, "an axis fully covered by footage has no nothing-captured gap")
    }

    // MARK: - Accessibility: every region type announces

    func testAccessibilityAnnouncesEveryRegionType() {
        let band = DayStripSegment(
            recording: "rec-a", taskIndex: 0, name: "Payroll run", category: "finance",
            startMs: dayStart, endMs: dayStart + hour
        )
        XCTAssertTrue(DayStripAccessibility.segmentLabel(band).hasPrefix("Payroll run, task, "))

        let base = DayStripBaseTrack(
            recording: "rec-a", title: "Morning session", startMs: dayStart, endMs: dayStart + hour
        )
        let baseLabel = DayStripAccessibility.baseTrackLabel(base)
        XCTAssertTrue(baseLabel.hasPrefix("Morning session, "))
        XCTAssertTrue(baseLabel.contains("unsplit, still searchable"))

        // U6: the legacy blanket `gapLabel` is retired — gaps speak through the
        // U5 cause labels ("Nothing on file, …" is the honest data claim).
        let gapLabel = DayStripAccessibility.gapCauseLabel(
            .nothingOnFile, startMs: dayStart, endMs: dayStart + hour
        )
        XCTAssertEqual(gapLabel?.hasPrefix("Nothing on file, "), true)

        let blocked = DayStripBlockedBand(startMs: dayStart, endMs: dayStart + hour)
        XCTAssertTrue(DayStripAccessibility.blockedLabel(blocked).hasPrefix("Blocked at capture, "))
    }
}
