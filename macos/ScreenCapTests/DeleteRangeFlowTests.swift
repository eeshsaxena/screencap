import XCTest
@testable import ScreenCap

/// U9 (R8, R19, R20, AE2) — pure coverage for the range-delete UX: the
/// confirm-sheet content model built from the `delete.start --dry_run` preview,
/// the deleted-band mapping, the state-machine computed properties, the failure
/// copy, and the wire decode of the new delete verbs + the `deleted` timeline
/// field. Render-free — the confirm sheet and the job wiring live in the view.
final class DeleteRangeFlowTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    // MARK: - DeleteConfirmContent.from(preview:)

    private let previewJSON = """
    {
      "ok": true, "schema_version": 1, "daemon_version": "test", "api_schema_version": 1,
      "start_ms": 1000, "end_ms": 8000,
      "recordings": [
        {"recording": "rec-a", "recording_id": "rid-a", "chunk_indices": [0, 1],
         "rounded_start_ms": 900, "rounded_end_ms": 4500,
         "excluded_live_chunks": [],
         "kept_clips": [{"id": "clip-1", "source_recording": "rec-a",
                         "source_day": "2026-07-18", "start_ms": 1200, "end_ms": 1800,
                         "created_at": 1.0, "creator": "ui"}]},
        {"recording": "rec-b", "chunk_indices": [3],
         "rounded_start_ms": 4600, "rounded_end_ms": 6000,
         "excluded_live_chunks": [4]}
      ],
      "resolved": {"rec-a": [0, 1], "rec-b": [3]},
      "total_chunks": 3
    }
    """

    func testConfirmContentUnionsRoundedExtentAcrossRecordings() throws {
        let preview = try decode(DeletePreviewResponse.self, previewJSON)
        let content = DeleteConfirmContent.from(preview: preview)
        // The removed extent is the union of the per-recording rounded extents.
        XCTAssertEqual(content.roundedStartMs, 900)
        XCTAssertEqual(content.roundedEndMs, 6000)
        XCTAssertEqual(content.extentStartMs, 900)
        XCTAssertEqual(content.extentEndMs, 6000)
        XCTAssertEqual(content.totalChunks, 3)
        XCTAssertEqual(content.recordingCount, 2)
        XCTAssertTrue(content.isDeletable)
    }

    func testConfirmContentDisclosesKeptClipsAndLiveExclusion() throws {
        let preview = try decode(DeletePreviewResponse.self, previewJSON)
        let content = DeleteConfirmContent.from(preview: preview)
        // R20: overlapping clips are disclosed (kept), flattened across recordings.
        XCTAssertEqual(content.keptClips.count, 1)
        XCTAssertEqual(content.keptClips.first?.clipId, "clip-1")
        XCTAssertEqual(content.keptClips.first?.startMs, 1200)
        // The live in-flight chunk (rec-b) was excluded → honest partial.
        XCTAssertTrue(content.hasExcludedLiveChunks)
    }

    func testConfirmContentCarriesTheConfirmTokenVerbatim() throws {
        let preview = try decode(DeletePreviewResponse.self, previewJSON)
        let content = DeleteConfirmContent.from(preview: preview)
        // The resolved map is passed to execute unchanged (no TOCTOU).
        XCTAssertEqual(content.resolved["rec-a"], [0, 1])
        XCTAssertEqual(content.resolved["rec-b"], [3])
    }

    func testConfirmContentEmptyRangeIsNotDeletable() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "test", "api_schema_version": 1,
          "start_ms": 2000, "end_ms": 3000,
          "recordings": [], "resolved": {}, "total_chunks": 0
        }
        """
        let preview = try decode(DeletePreviewResponse.self, json)
        let content = DeleteConfirmContent.from(preview: preview)
        XCTAssertFalse(content.isDeletable)
        // No rounded extent → the clock falls back to the requested range, never
        // a false extent.
        XCTAssertNil(content.roundedStartMs)
        XCTAssertEqual(content.extentStartMs, 2000)
        XCTAssertEqual(content.extentEndMs, 3000)
        XCTAssertTrue(content.extentClockText.contains("–"))
    }

    // MARK: - Deleted-band mapping (R8)

    func testDeletedBandsMapFromSpans() throws {
        let spanJSON = """
        {"name": "rec-a", "state": "ready", "start_ms": 1000, "end_ms": 9000,
         "blocked_proven": [], "unverifiable": [],
         "deleted": [{"start_ms": 2000, "end_ms": 3000}, {"start_ms": 5000, "end_ms": 5500}]}
        """
        let span = try decode(DaySegmentRecording.self, spanJSON)
        let bands = DayDeletedInterval.bands(from: [span])
        XCTAssertEqual(bands.count, 2)
        XCTAssertEqual(bands[0].startMs, 2000)
        XCTAssertEqual(bands[0].endMs, 3000)
        XCTAssertEqual(bands[1].startMs, 5000)
    }

    func testDaySegmentDeletedDefaultsEmptyForOlderDaemon() throws {
        // A pre-U8 daemon omits `deleted` entirely — must decode to [], never fail.
        let spanJSON = """
        {"name": "rec-a", "state": "ready", "start_ms": 1000, "end_ms": 2000,
         "blocked_proven": [], "unverifiable": []}
        """
        let span = try decode(DaySegmentRecording.self, spanJSON)
        XCTAssertEqual(span.deleted, [])
        XCTAssertTrue(DayDeletedInterval.bands(from: [span]).isEmpty)
    }

    // MARK: - Deleted band is a distinct legend class (R8)

    func testLegendCarriesRemovedByYouDistinctFromRemovedByYourRules() {
        let texts = DayStripLegend.items.map(\.text)
        XCTAssertTrue(texts.contains("removed by you"))
        XCTAssertTrue(texts.contains("removed by your rules"))
        // The two are distinct entries with distinct swatches (R8).
        let byYou = DayStripLegend.items.first { $0.text == "removed by you" }
        let byRules = DayStripLegend.items.first { $0.text == "removed by your rules" }
        XCTAssertEqual(byYou?.swatch, .removedByYou)
        XCTAssertEqual(byRules?.swatch, .purged)
        XCTAssertNotEqual(byYou?.swatch, byRules?.swatch)
    }

    func testDeletedAccessibilityCopySaysRemovedFromThisMac() {
        let label = DayStripAccessibility.deletedLabel(
            DayDeletedInterval(startMs: 1000, endMs: 2000)
        )
        XCTAssertTrue(label.contains("Removed from this Mac"))
    }

    // MARK: - State machine (Confirm → Deleting → {Idle | Error})

    func testPhaseSheetPresentedForConfirmingAndDeleting() throws {
        let content = DeleteConfirmContent.from(
            preview: try decode(DeletePreviewResponse.self, previewJSON)
        )
        XCTAssertTrue(DeleteRangePhase.confirming(content).sheetPresented)
        XCTAssertTrue(DeleteRangePhase.deleting(fraction: 0.5).sheetPresented)
        XCTAssertFalse(DeleteRangePhase.idle.sheetPresented)
        XCTAssertFalse(DeleteRangePhase.failed(message: "x").sheetPresented)
    }

    func testPhaseExposesConfirmContentAndFraction() throws {
        let content = DeleteConfirmContent.from(
            preview: try decode(DeletePreviewResponse.self, previewJSON)
        )
        XCTAssertEqual(DeleteRangePhase.confirming(content).confirmContent, content)
        XCTAssertNil(DeleteRangePhase.deleting(fraction: 0.25).confirmContent)
        XCTAssertEqual(DeleteRangePhase.deleting(fraction: 0.25).deletingFraction, 0.25)
        XCTAssertNil(DeleteRangePhase.confirming(content).deletingFraction)
    }

    func testPhaseFailureMessageOnlyWhenFailed() {
        XCTAssertEqual(DeleteRangePhase.failed(message: "boom").failureMessage, "boom")
        XCTAssertNil(DeleteRangePhase.idle.failureMessage)
        XCTAssertNil(DeleteRangePhase.deleting(fraction: nil).failureMessage)
    }

    // MARK: - Failure copy (R19 — never silent)

    func testReconfirmCopyTellsUserToReselect() {
        XCTAssertTrue(DeleteRangeFailure.reconfirm.lowercased().contains("select the range again"))
        XCTAssertTrue(DeleteRangeFailure.reconfirm.lowercased().contains("nothing was removed"))
    }

    func testFailureFromSealedStoreError() {
        let msg = DeleteRangeFailure.fromError(
            DaemonClientError.envelopeError(code: "store_locked", rawBody: Data())
        )
        XCTAssertTrue(msg.lowercased().contains("locked"))
        XCTAssertTrue(msg.lowercased().contains("nothing was removed"))
    }

    func testJobFailureCopyDistinguishesCancel() {
        XCTAssertTrue(DeleteRangeFailure.job(state: "cancelled").lowercased().contains("cancelled"))
        XCTAssertTrue(DeleteRangeFailure.job(state: "failed").lowercased().contains("couldn't"))
    }

    // MARK: - Wire decode of the delete verbs

    func testDeleteStatusDecodeAndFraction() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "test", "api_schema_version": 1,
         "state": "running", "done": 2, "total": 5, "current_unit_index": 1,
         "deleted_chunks": 2, "reconfirm_required": false}
        """
        let status = try decode(DeleteStatusResponse.self, json)
        XCTAssertEqual(status.state, "running")
        XCTAssertEqual(status.done, 2)
        XCTAssertEqual(status.total, 5)
        XCTAssertEqual(status.deletedChunks, 2)
        XCTAssertFalse(status.reconfirmRequired)
        XCTAssertEqual(status.fraction, 0.4)
    }

    func testDeleteStatusFractionNilBeforeTotalKnown() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "test", "api_schema_version": 1,
         "state": "idle", "done": 0, "total": 0, "current_unit_index": 0,
         "deleted_chunks": 0, "reconfirm_required": false}
        """
        let status = try decode(DeleteStatusResponse.self, json)
        XCTAssertNil(status.fraction)
    }

    func testDeleteStartRequestEncodesWireKeys() throws {
        let body = try JSONEncoder().encode(
            DeleteStartRequest(startMs: 10, endMs: 20, dryRun: false, resolved: ["rec-a": [0, 1]])
        )
        let object = try JSONSerialization.jsonObject(with: body) as? [String: Any]
        XCTAssertEqual(object?["start_ms"] as? Int, 10)
        XCTAssertEqual(object?["end_ms"] as? Int, 20)
        XCTAssertEqual(object?["dry_run"] as? Bool, false)
        let resolved = object?["resolved"] as? [String: [Int]]
        XCTAssertEqual(resolved?["rec-a"], [0, 1])
    }
}
