import XCTest
@testable import Screencap

/// U2 (prototype UI) — the additive `RecordingSummary` fields. The decoder must
/// accept payloads WITH the new fields (a current daemon) and WITHOUT them (an
/// older daemon on disk), never gating readiness on their presence.
final class RecordingSummaryDecodeTests: XCTestCase {

    private func decode(_ json: String) throws -> RecordingSummary {
        try JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }

    func testDecodesPayloadWithAllNewFields() throws {
        let rec = try decode("""
        {
          "name": "stripe-webhook-debugging",
          "date": "2026-07-03", "duration": "0:42", "size_mb": "12.0 MB",
          "has_audio": true, "transcribed": false, "uploaded": false, "is_stub": false,
          "chunks_total": 3, "chunks_uploaded": 0, "intent": "local",
          "started_at": 1751536800.0, "duration_seconds": 42.0, "drops": null,
          "size_bytes": 12582912,
          "summary": "User debugged Stripe webhooks in Django.",
          "title": "Stripe Webhook Debugging",
          "state": "processing",
          "recording_id": "2026-07-03_10-04-32"
        }
        """)
        XCTAssertEqual(rec.sizeBytes, 12_582_912)
        XCTAssertEqual(rec.summary, "User debugged Stripe webhooks in Django.")
        XCTAssertEqual(rec.title, "Stripe Webhook Debugging")
        XCTAssertEqual(rec.state, "processing")
        XCTAssertTrue(rec.isProcessing)
        XCTAssertFalse(rec.isReady)
        XCTAssertEqual(rec.recordingId, "2026-07-03_10-04-32")
        // Identity survives the auto-name rename via the pinned recording_id.
        XCTAssertEqual(rec.stableID, "2026-07-03_10-04-32")
    }

    func testDecodesOlderPayloadWithoutNewFieldsUsingDefaults() throws {
        // No size_bytes / summary / title / state / recording_id — an older daemon.
        let rec = try decode("""
        {
          "name": "legacy-rec",
          "date": "2026-05-28", "duration": "0:42", "size_mb": "1.2 MB",
          "has_audio": false, "transcribed": false, "uploaded": false, "is_stub": false,
          "chunks_total": 0, "chunks_uploaded": 0, "intent": null,
          "started_at": 1748390400.0, "duration_seconds": 42.0, "drops": null
        }
        """)
        XCTAssertEqual(rec.sizeBytes, 0)
        XCTAssertNil(rec.summary)
        XCTAssertEqual(rec.title, "legacy-rec", "title falls back to the directory name")
        XCTAssertEqual(rec.state, "ready", "a missing state defaults to ready, never a spinner")
        XCTAssertTrue(rec.isReady)
        XCTAssertNil(rec.recordingId)
        XCTAssertEqual(rec.stableID, "legacy-rec", "stableID falls back to name")
    }

    func testActivelyRecordingState() throws {
        let rec = try decode("""
        {
          "name": "live", "date": "2026-07-03", "duration": "0:05", "size_mb": "0.1 MB",
          "has_audio": true, "transcribed": false, "uploaded": false, "is_stub": false,
          "chunks_total": 0, "chunks_uploaded": 0, "intent": null,
          "started_at": 1751536800.0, "duration_seconds": 5.0, "drops": null,
          "state": "recording"
        }
        """)
        XCTAssertTrue(rec.isActivelyRecording)
        XCTAssertFalse(rec.isProcessing)
        XCTAssertFalse(rec.isReady)
    }
}
