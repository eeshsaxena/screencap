import XCTest
@testable import ScreenCap

/// Editable titles (U6) — the pure Rename… rules (`RenameModel`) and the additive
/// `title_is_user_set` decode on `RecordingSummary`. No render: every assertion
/// is on the pure model, mirroring `LibraryFilterTests`.
final class LibraryCardRenameTests: XCTestCase {

    // MARK: - No-op-on-unchanged-default (the freeze-guard)

    /// Opening Rename… on a default-titled recording and hitting Save without
    /// editing must NOT write — else the date/time default freezes as a user
    /// title. Absent flag (older daemon → nil) counts as not-user-set.
    func testUnchangedDerivedDefaultIsNoOp() {
        XCTAssertEqual(
            RenameModel.decide(draft: "3 Jul 2026 · 10:04",
                               currentTitle: "3 Jul 2026 · 10:04",
                               titleIsUserSet: false),
            .noOp
        )
        XCTAssertEqual(
            RenameModel.decide(draft: "3 Jul 2026 · 10:04",
                               currentTitle: "3 Jul 2026 · 10:04",
                               titleIsUserSet: nil),
            .noOp
        )
    }

    /// A user-set title re-submitted unchanged is a real (idempotent) write —
    /// never the default-freeze no-op path.
    func testUnchangedUserTitleStillSubmits() {
        XCTAssertEqual(
            RenameModel.decide(draft: "Stripe debugging",
                               currentTitle: "Stripe debugging",
                               titleIsUserSet: true),
            .submit("Stripe debugging")
        )
    }

    func testEditedTitleSubmits() {
        XCTAssertEqual(
            RenameModel.decide(draft: "New title",
                               currentTitle: "3 Jul 2026 · 10:04",
                               titleIsUserSet: false),
            .submit("New title")
        )
    }

    /// Empty is a valid submission (clears back to the default), never a no-op,
    /// for both a default and a user-set current title.
    func testClearingToEmptySubmits() {
        XCTAssertEqual(
            RenameModel.decide(draft: "", currentTitle: "3 Jul 2026 · 10:04", titleIsUserSet: false),
            .submit("")
        )
        XCTAssertEqual(
            RenameModel.decide(draft: "", currentTitle: "Stripe debugging", titleIsUserSet: true),
            .submit("")
        )
    }

    // MARK: - Live 200-char cap

    func testCapEnforcesTwoHundredCharacters() {
        let long = String(repeating: "a", count: 250)
        XCTAssertEqual(RenameModel.cap(long).count, 200)
        let short = "short title"
        XCTAssertEqual(RenameModel.cap(short), short)
    }

    /// The decision caps before submitting, so an over-length paste can never
    /// reach the verb un-truncated.
    func testDecideCapsBeforeSubmitting() {
        let long = String(repeating: "z", count: 250)
        guard case .submit(let title) = RenameModel.decide(
            draft: long, currentTitle: "default", titleIsUserSet: false
        ) else {
            return XCTFail("expected submit")
        }
        XCTAssertEqual(title.count, 200)
    }

    // MARK: - title_is_user_set decode (additive, nullable)

    func testDecodesTitleIsUserSet() throws {
        XCTAssertEqual(try decode(userSet: "true").titleIsUserSet, true)
        XCTAssertEqual(try decode(userSet: "false").titleIsUserSet, false)
        // Absent key → nil (older daemon), treated as not-user-set downstream.
        XCTAssertNil(try decode(userSet: nil).titleIsUserSet)
    }

    private func decode(userSet: String?) throws -> RecordingSummary {
        let line = userSet.map { ",\n          \"title_is_user_set\": \($0)" } ?? ""
        let json = """
        {
          "name": "rec-1",
          "date": "2026-07-03", "duration": "0:42", "size_mb": "1.2",
          "has_audio": false, "transcribed": false, "uploaded": false, "is_stub": false,
          "chunks_total": 0, "chunks_uploaded": 0, "intent": null,
          "started_at": 1751536800.0, "duration_seconds": 42.0, "drops": null,
          "size_bytes": 1258291, "summary": null,
          "title": "3 Jul 2026 · 10:04", "state": "ready"\(line)
        }
        """
        return try JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }
}
