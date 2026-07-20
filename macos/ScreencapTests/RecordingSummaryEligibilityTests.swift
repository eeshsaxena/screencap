import XCTest
@testable import Screencap

/// U4 — pins the `isUploadEligible` predicate (plan R2). The truth table is
/// load-bearing: the row view conditionally renders the Upload button off
/// this property, and any drift between the predicate and AE1/AE2 would
/// silently regress the visibility contract.
@MainActor
final class RecordingSummaryEligibilityTests: XCTestCase {
    func testEligibleWhenNotUploadedAndNotStub() {
        let rec = makeSummary(uploaded: false, isStub: false)
        XCTAssertTrue(rec.isUploadEligible)
    }

    /// Covers AE1 — uploaded recordings never show the Upload affordance.
    func testIneligibleWhenAlreadyUploaded() {
        let rec = makeSummary(uploaded: true, isStub: false)
        XCTAssertFalse(rec.isUploadEligible)
    }

    /// Covers AE2 — stub recordings (uploaded + local files removed) never
    /// show the Upload affordance even if the `uploaded` flag were somehow
    /// false (defense in depth — the stub state implies an upload happened).
    func testIneligibleWhenStub() {
        let rec = makeSummary(uploaded: false, isStub: true)
        XCTAssertFalse(rec.isUploadEligible)
    }

    /// The (uploaded=true, isStub=true) combination is the on-disk-deleted-
    /// after-successful-upload state and should obviously be ineligible.
    /// Asserted explicitly so a future refactor that reduces the predicate
    /// to `!uploaded` (and drops the `!isStub` clause) is caught.
    func testIneligibleWhenBothUploadedAndStub() {
        let rec = makeSummary(uploaded: true, isStub: true)
        XCTAssertFalse(rec.isUploadEligible)
    }

    /// SCR-163 — eligibility must be **intent-agnostic**: it depends ONLY on
    /// upload state (`!uploaded && !isStub`), never on the recording's intent.
    /// This is the load-bearing recovery floor: the daemon's auto-resume is
    /// gated on cloud/both intent (`supervisor._is_cloud_recording`), so the
    /// Recordings-list Upload button is the ONLY recovery path for local/legacy/
    /// no-intent recordings (manual `screencap upload` force-promotes them to
    /// cloud). A future change like `&& intent == "cloud"` would silently strand
    /// every non-cloud recording behind a closed `.refused` window — exactly the
    /// gap this ticket verified does not currently exist. Pin it so it can't
    /// regress in.
    func testEligibilityIsIndependentOfIntent() {
        for intent in ["cloud", "local", "both", nil] {
            let rec = makeSummary(uploaded: false, isStub: false, intent: intent)
            XCTAssertTrue(
                rec.isUploadEligible,
                "a not-uploaded, non-stub recording must be eligible regardless "
                    + "of intent (intent: \(intent ?? "nil"))"
            )
        }
    }

    // MARK: - Helpers

    /// Builds a `RecordingSummary` by routing through its real Decodable
    /// init so the test exercises the same path the `screencap list --json`
    /// consumer takes — avoids drifting if the model's decoding logic
    /// changes (e.g. a new required field, a snake_case mapping update).
    private func makeSummary(
        uploaded: Bool,
        isStub: Bool,
        intent: String? = nil
    ) -> RecordingSummary {
        let intentJSON = intent.map { "\"\($0)\"" } ?? "null"
        let json = """
        {
          "name": "rec-test",
          "date": "2026-05-28",
          "duration": "0:42",
          "size_mb": "1.2",
          "has_audio": false,
          "transcribed": false,
          "uploaded": \(uploaded),
          "is_stub": \(isStub),
          "chunks_total": 0,
          "chunks_uploaded": 0,
          "intent": \(intentJSON),
          "started_at": 1748390400.0,
          "duration_seconds": 42.0,
          "drops": null
        }
        """
        let data = Data(json.utf8)
        return try! JSONDecoder().decode(RecordingSummary.self, from: data)
    }
}
