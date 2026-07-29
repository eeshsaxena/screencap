import Foundation
import XCTest
@testable import Screencap

/// SCR-299 U3 — the pure rules the Inspect share menu renders from: which
/// recordings can be shared, whether a live link exists, and what a failure
/// tells the user to do. No SwiftUI render, no daemon.
final class ShareLinkPolicyTests: XCTestCase {

    // MARK: - Eligibility (R11 / KTD3)

    func testUploadedRecordingIsShareableAndNeverUploadedIsNot() {
        XCTAssertTrue(makeSummary(uploaded: true).isShareable)
        XCTAssertFalse(makeSummary(uploaded: false).isShareable)
    }

    /// A stub is the uploaded-then-locally-deleted state. Its cloud copy is
    /// exactly what a share reads, so the predicate must not be the thing that
    /// excludes it — the regression this pins would silently disable sharing on
    /// every evicted recording.
    func testStubIsStillShareable() {
        XCTAssertTrue(makeSummary(uploaded: true, isStub: true).isShareable)
    }

    /// Guards the inverse relationship with the neighbouring predicate: a
    /// recording is never both upload-eligible and shareable.
    func testShareabilityIsTheInverseOfUploadEligibility() {
        let uploaded = makeSummary(uploaded: true)
        let notUploaded = makeSummary(uploaded: false)
        XCTAssertTrue(uploaded.isShareable && !uploaded.isUploadEligible)
        XCTAssertTrue(!notUploaded.isShareable && notUploaded.isUploadEligible)
    }

    // MARK: - Share state (R14 / KTD4)

    func testNoRecordsMeansNoActiveLink() {
        XCTAssertEqual(ShareLinkPolicy.state(for: "rec-1", in: []), .none)
    }

    func testMatchingLiveRecordYieldsItsToken() {
        let records = [record(token: "tok-1", recording: "rec-1", expiresAt: futureStamp)]
        XCTAssertEqual(
            ShareLinkPolicy.state(for: "rec-1", in: records, now: now),
            .active(token: "tok-1")
        )
    }

    /// The local store holds every share this Mac made, across recordings.
    func testRecordsForOtherRecordingsAreIgnored() {
        let records = [record(token: "tok-1", recording: "rec-OTHER", expiresAt: futureStamp)]
        XCTAssertEqual(ShareLinkPolicy.state(for: "rec-1", in: records, now: now), .none)
    }

    func testRevokedRecordIsNotActive() {
        let records = [
            record(token: "tok-1", recording: "rec-1", expiresAt: futureStamp, revoked: true)
        ]
        XCTAssertEqual(ShareLinkPolicy.state(for: "rec-1", in: records, now: now), .none)
    }

    func testExpiredRecordIsNotActive() {
        let records = [record(token: "tok-1", recording: "rec-1", expiresAt: pastStamp)]
        XCTAssertEqual(ShareLinkPolicy.state(for: "rec-1", in: records, now: now), .none)
    }

    /// Several creates mint several tokens; a dead one must not mask a live one.
    func testLiveRecordWinsOverRevokedAndExpiredSiblings() {
        let records = [
            record(token: "dead", recording: "rec-1", expiresAt: pastStamp),
            record(token: "gone", recording: "rec-1", expiresAt: futureStamp, revoked: true),
            record(token: "live", recording: "rec-1", expiresAt: futureStamp),
        ]
        XCTAssertEqual(
            ShareLinkPolicy.state(for: "rec-1", in: records, now: now),
            .active(token: "live")
        )
    }

    /// Fractional seconds appear only when Python's isoformat emits them, so
    /// both shapes have to parse or expiry silently fails open for one of them.
    func testExpiryParsesWithAndWithoutFractionalSeconds() {
        XCTAssertTrue(ShareLinkPolicy.isExpired("2026-01-01T00:00:00+00:00", now: now))
        XCTAssertTrue(ShareLinkPolicy.isExpired("2026-01-01T00:00:00.123456+00:00", now: now))
        XCTAssertFalse(ShareLinkPolicy.isExpired("2027-01-01T00:00:00+00:00", now: now))
        XCTAssertFalse(ShareLinkPolicy.isExpired("2027-01-01T00:00:00.123456+00:00", now: now))
    }

    /// Fails OPEN, unlike the server's fail-closed reading of the same field.
    /// Hiding Revoke would strand a user unable to stop sharing a live link;
    /// offering it on a dead one costs nothing.
    func testUnparseableOrMissingExpiryIsTreatedAsLive() {
        XCTAssertFalse(ShareLinkPolicy.isExpired(nil, now: now))
        XCTAssertFalse(ShareLinkPolicy.isExpired("not-a-timestamp", now: now))

        let records = [record(token: "tok-1", recording: "rec-1", expiresAt: "garbage")]
        XCTAssertEqual(
            ShareLinkPolicy.state(for: "rec-1", in: records, now: now),
            .active(token: "tok-1")
        )
    }

    // MARK: - Failure copy (R13 / KTD2)

    func testEachTypedCodeMapsToItsOwnRecovery() {
        XCTAssertEqual(ShareLinkErrorCopy.fromCode("not_signed_in"), .notSignedIn)
        XCTAssertEqual(ShareLinkErrorCopy.fromCode("share_unavailable"), .nothingToShare)
        XCTAssertEqual(ShareLinkErrorCopy.fromCode("share_backend_unavailable"), .backendUnavailable)

        XCTAssertEqual(ShareLinkErrorCopy.notSignedIn.action, .signIn)
        XCTAssertEqual(ShareLinkErrorCopy.backendUnavailable.action, .retry)
        // Retrying cannot make an un-uploaded recording shareable.
        XCTAssertEqual(ShareLinkErrorCopy.nothingToShare.action, .none)
    }

    func testUnknownAndFutureCodesFallBackWithoutSurfacingText() {
        XCTAssertEqual(ShareLinkErrorCopy.fromCode("some_future_code"), .unknown)
        XCTAssertEqual(ShareLinkErrorCopy.fromCode(""), .unknown)
    }

    func testTransportFailuresAreDistinguishedFromBackendFailures() {
        XCTAssertEqual(
            ShareLinkErrorCopy.resolve(DaemonClientError.socketUnavailable(path: "/tmp/x.sock")),
            .helperUnavailable
        )
        XCTAssertEqual(
            ShareLinkErrorCopy.resolve(DaemonClientError.timedOut(seconds: 300)),
            .backendUnavailable
        )
        XCTAssertEqual(
            ShareLinkErrorCopy.resolve(
                DaemonClientError.envelopeError(code: "not_signed_in", rawBody: Data())
            ),
            .notSignedIn
        )
    }

    /// The whole point of keying on codes: no rendered message may quote the
    /// backend. A raw server string reaching the UI is the failure mode
    /// `AccountErrorCopy` exists to prevent, and this rule inherits it.
    func testNoMessageEmbedsBackendText() {
        let leak = "RAW-BACKEND-TEXT-9000"
        let copy = ShareLinkErrorCopy.resolve(
            DaemonClientError.envelopeError(code: leak, rawBody: Data(leak.utf8))
        )
        XCTAssertEqual(copy, .unknown)
        for candidate in ShareLinkErrorCopy.allCases {
            XCTAssertFalse(candidate.message.contains(leak))
            XCTAssertFalse(candidate.message.isEmpty)
        }
    }

    // MARK: - Fixtures

    private let now = Date(timeIntervalSince1970: 1_780_000_000)  // 2026-05-29
    private let pastStamp = "2026-01-01T00:00:00+00:00"
    private let futureStamp = "2027-01-01T00:00:00+00:00"

    private func record(
        token: String,
        recording: String,
        expiresAt: String?,
        revoked: Bool? = nil
    ) -> ShareRecord {
        // Built key-by-key rather than with an `as Any` literal: a nil bridged
        // through `Any` is not NSNull, so a literal would crash serialization
        // instead of omitting the key the way the daemon's record does.
        var payload: [String: Any] = ["token": token, "recording": recording]
        if let expiresAt { payload["expires_at"] = expiresAt }
        if let revoked { payload["revoked"] = revoked }
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return try! JSONDecoder().decode(ShareRecord.self, from: data)
    }

    private func makeSummary(uploaded: Bool, isStub: Bool = false) -> RecordingSummary {
        let payload: [String: Any] = [
            "name": "rec-1",
            "date": "2026-05-29",
            "duration": "00:01:00",
            "size_mb": "10",
            "has_audio": false,
            "transcribed": false,
            "uploaded": uploaded,
            "is_stub": isStub,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return try! JSONDecoder().decode(RecordingSummary.self, from: data)
    }
}
