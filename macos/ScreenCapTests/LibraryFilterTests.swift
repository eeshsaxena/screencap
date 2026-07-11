import XCTest
@testable import ScreenCap

/// U5 — the Library grid's pure model: chip → row membership (KTD-6), the
/// honesty-substituted status badge (KTD-9), and the visible-rows ordering +
/// identity. No render; every assertion is on `LibraryChip` / `LibraryBadge` /
/// `LibraryGrid`.
@MainActor
final class LibraryFilterTests: XCTestCase {

    // MARK: - Chip membership (KTD-6)

    func testAllChipMatchesEveryRow() {
        for rec in [uploadedReady, localReady, localProcessing, activeRecording] {
            XCTAssertTrue(LibraryChip.all.matches(rec), "All must match \(rec.state)/\(rec.uploaded)")
        }
    }

    func testLocalChipIsNotUploaded() {
        XCTAssertTrue(LibraryChip.local.matches(localReady))
        XCTAssertTrue(LibraryChip.local.matches(localProcessing))
        XCTAssertTrue(LibraryChip.local.matches(activeRecording))
        XCTAssertFalse(LibraryChip.local.matches(uploadedReady))
    }

    func testUploadedChipIsUploadedOnly() {
        XCTAssertTrue(LibraryChip.uploaded.matches(uploadedReady))
        XCTAssertFalse(LibraryChip.uploaded.matches(localReady))
        XCTAssertFalse(LibraryChip.uploaded.matches(localProcessing))
        XCTAssertFalse(LibraryChip.uploaded.matches(activeRecording))
    }

    func testNeedsReviewChipIsNotReady() {
        XCTAssertTrue(LibraryChip.needsReview.matches(localProcessing))
        XCTAssertTrue(LibraryChip.needsReview.matches(activeRecording))
        XCTAssertFalse(LibraryChip.needsReview.matches(localReady))
        XCTAssertFalse(LibraryChip.needsReview.matches(uploadedReady))
    }

    /// A processing local recording deliberately appears under both Local and
    /// Needs review, mirroring the prototype's draft card (logic 710–711).
    func testProcessingLocalRowIsBothLocalAndNeedsReview() {
        XCTAssertTrue(LibraryChip.local.matches(localProcessing))
        XCTAssertTrue(LibraryChip.needsReview.matches(localProcessing))
    }

    // MARK: - Badge mapping (KTD-9)

    func testBadgeMapping() {
        XCTAssertEqual(LibraryBadge.forRecording(uploadedReady), LibraryBadge(text: "uploaded", tone: .uploaded))
        XCTAssertEqual(LibraryBadge.forRecording(localReady), LibraryBadge(text: "local", tone: .local))
        XCTAssertEqual(LibraryBadge.forRecording(localProcessing), LibraryBadge(text: "draft · local", tone: .draft))
        XCTAssertEqual(LibraryBadge.forRecording(activeRecording), LibraryBadge(text: "draft · local", tone: .draft))
    }

    /// Uploaded wins over not-yet-ready so a cloud recording still finalizing
    /// reads "uploaded", never a fabricated draft.
    func testUploadedBeatsProcessing() {
        let rec = makeSummary(uploaded: true, state: "processing")
        XCTAssertEqual(LibraryBadge.forRecording(rec).tone, .uploaded)
    }

    // MARK: - Encrypted badge (R3 / KTD-4)

    /// AE1 (badge half): a frozen-on uploaded recording badges its encryption.
    func testFrozenOnUploadedBadgesEncrypted() {
        let rec = makeSummary(uploaded: true, state: "ready", cloudE2EE: true)
        XCTAssertEqual(LibraryBadge.forRecording(rec), LibraryBadge(text: "uploaded · encrypted", tone: .uploaded))
    }

    /// AE3: frozen-off and absent (old daemon) recordings never claim
    /// encryption — absent decodes to nil and yields today's badge unchanged.
    func testFrozenOffOrAbsentNeverClaimsEncryption() {
        let frozenOff = makeSummary(uploaded: true, state: "ready", cloudE2EE: false)
        XCTAssertEqual(LibraryBadge.forRecording(frozenOff), LibraryBadge(text: "uploaded", tone: .uploaded))
        let absent = makeSummary(uploaded: true, state: "ready", cloudE2EE: nil)
        XCTAssertNil(absent.cloudE2EE)
        XCTAssertEqual(LibraryBadge.forRecording(absent), LibraryBadge(text: "uploaded", tone: .uploaded))
    }

    /// The badge is about uploaded copies: a local recording never badges
    /// encryption, even if the frozen bit is (anomalously) true.
    func testLocalRecordingIgnoresFrozenBit() {
        for state in ["ready", "processing", "recording"] {
            let rec = makeSummary(uploaded: false, state: state, cloudE2EE: true)
            XCTAssertFalse(
                LibraryBadge.forRecording(rec).text.lowercased().contains("encrypted"),
                "local \(state) row must not claim encryption"
            )
        }
    }

    /// AE3 (mixed library): each row renders its own frozen truth.
    func testMixedLibraryRendersPerRecordingTruth() {
        let encrypted = makeSummary(name: "enc", uploaded: true, cloudE2EE: true)
        let plaintext = makeSummary(name: "plain", uploaded: true, cloudE2EE: false)
        XCTAssertEqual(LibraryBadge.forRecording(encrypted).text, "uploaded · encrypted")
        XCTAssertEqual(LibraryBadge.forRecording(plaintext).text, "uploaded")
    }

    /// KTD-9: "shared" stays forbidden until team semantics exist (SCR-221),
    /// and "encrypted" appears only from the frozen intent bit (KTD-4).
    func testBadgeNeverEmitsSharedAndEncryptedOnlyFromFrozenIntent() {
        for rec in [uploadedReady, localReady, localProcessing, activeRecording,
                    makeSummary(uploaded: true, state: "processing"),
                    makeSummary(uploaded: true, cloudE2EE: true),
                    makeSummary(uploaded: false, cloudE2EE: true)] {
            let text = LibraryBadge.forRecording(rec).text.lowercased()
            XCTAssertFalse(text.contains("shared"), "badge '\(text)' must not say 'shared'")
            if !(rec.uploaded && rec.cloudE2EE == true) {
                XCTAssertFalse(text.contains("encrypted"), "badge '\(text)' must not say 'encrypted'")
            }
        }
    }

    // MARK: - Ordering + identity

    /// The active/processing recording carries the newest `startedAt`, so it
    /// lands at the grid head (the design's draft-card injection).
    func testVisibleSortsDraftToHead() {
        let older = makeSummary(name: "older", uploaded: false, state: "ready", startedAt: 1000)
        let newest = makeSummary(name: "draft", uploaded: false, state: "processing", startedAt: 9000)
        let visible = LibraryGrid.visible([older, newest], chip: .all)
        XCTAssertEqual(visible.map(\.name), ["draft", "older"])
    }

    func testVisibleFiltersByChip() {
        let rows = [uploadedReady, localReady, localProcessing]
        XCTAssertEqual(Set(LibraryGrid.visible(rows, chip: .uploaded).map(\.name)), [uploadedReady.name])
        XCTAssertEqual(Set(LibraryGrid.visible(rows, chip: .needsReview).map(\.name)), [localProcessing.name])
    }

    /// Grid identity keys on `recording_id` so the post-stop auto-name directory
    /// rename updates a card in place instead of spawning a duplicate.
    func testStableIDPrefersRecordingID() {
        let withID = makeSummary(name: "dir-name", recordingId: "rec-abc")
        XCTAssertEqual(withID.stableID, "rec-abc")
        let legacy = makeSummary(name: "legacy", recordingId: nil)
        XCTAssertEqual(legacy.stableID, "legacy")
    }

    // MARK: - Fixtures

    private lazy var uploadedReady = makeSummary(name: "up", uploaded: true, state: "ready")
    private lazy var localReady = makeSummary(name: "loc", uploaded: false, state: "ready")
    private lazy var localProcessing = makeSummary(name: "proc", uploaded: false, state: "processing")
    private lazy var activeRecording = makeSummary(name: "rec", uploaded: false, state: "recording")

    /// `cloudE2EE: nil` omits the `cloud_e2ee` key entirely — the old-daemon
    /// wire shape — so the nil cases genuinely exercise the absent-key decode.
    private func makeSummary(
        name: String = "rec-test",
        uploaded: Bool = false,
        state: String = "ready",
        startedAt: Double = 1_748_390_400.0,
        recordingId: String? = nil,
        cloudE2EE: Bool? = nil
    ) -> RecordingSummary {
        let recIDJSON = recordingId.map { "\"\($0)\"" } ?? "null"
        let cloudE2EELine = cloudE2EE.map { ",\n          \"cloud_e2ee\": \($0)" } ?? ""
        let json = """
        {
          "name": "\(name)",
          "date": "2026-05-28",
          "duration": "0:42",
          "size_mb": "1.2",
          "has_audio": false,
          "transcribed": false,
          "uploaded": \(uploaded),
          "is_stub": false,
          "chunks_total": 0,
          "chunks_uploaded": 0,
          "intent": null,
          "started_at": \(startedAt),
          "duration_seconds": 42.0,
          "drops": null,
          "size_bytes": 1258291,
          "summary": null,
          "title": "\(name)",
          "state": "\(state)",
          "recording_id": \(recIDJSON)\(cloudE2EELine)
        }
        """
        return try! JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }
}
