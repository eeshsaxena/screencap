import XCTest
@testable import Screencap

/// Focused tests for `DaemonSessionService.translateFailure(_:)`. The
/// transport-level behaviors (probe outcomes, snapshot outcomes, event
/// stream reconnect/backoff, cursor recovery) are exercised end-to-end by
/// `RecorderControllerDaemonTests` via `UnixHTTPTestServer`.
@MainActor
final class DaemonSessionServiceTests: XCTestCase {
    func testTranslateSchemaMismatch() {
        let service = LiveDaemonSessionService()

        let outcome = service.translateFailure(DaemonClientError.schemaMismatch(expected: 1, got: 99))

        XCTAssertEqual(outcome, .schemaMismatch)
    }

    func testTranslateSocketUnavailable() {
        let service = LiveDaemonSessionService()

        let outcome = service.translateFailure(DaemonClientError.socketUnavailable(path: "/tmp/x"))

        XCTAssertEqual(outcome, .socketUnavailable)
    }

    func testTranslateConnectionFailed() {
        let service = LiveDaemonSessionService()
        struct Boom: Error {}

        let outcome = service.translateFailure(DaemonClientError.connectionFailed(underlying: Boom()))

        XCTAssertEqual(outcome, .socketUnavailable)
    }

    func testTranslateLockContendedEnvelopeError() {
        let service = LiveDaemonSessionService()
        let error = DaemonClientError.envelopeError(code: DaemonErrorCode.lockContended, rawBody: Data())

        XCTAssertEqual(service.translateFailure(error), .lockContended)
    }

    func testTranslateNotOwnedByDaemonEnvelopeError() {
        let service = LiveDaemonSessionService()
        let error = DaemonClientError.envelopeError(code: DaemonErrorCode.notOwnedByDaemon, rawBody: Data())

        XCTAssertEqual(service.translateFailure(error), .lockContended)
    }

    // SCR-228: recording.start refused during a storage migration surfaces the
    // honest "a move is already in progress" copy, not a generic system error.
    func testTranslateMigrationInProgressCarriesHonestCopy() {
        let service = LiveDaemonSessionService()
        let error = DaemonClientError.envelopeError(
            code: DaemonErrorCode.migrationInProgress, rawBody: Data()
        )
        guard case .other(let description) = service.translateFailure(error) else {
            return XCTFail("expected .other")
        }
        XCTAssertEqual(
            description,
            PrivacySettingsPolicy.migrationFailureFallback(reason: "migration_in_progress")
        )
        XCTAssertFalse(description.contains("_"))  // human copy, not a raw code
    }

    // SCR-276: the post-stop finalize drain must NOT collapse into
    // `.lockContended` — nothing is recording, so that case's "Screencap is
    // already recording." copy is wrong. It routes to its own case, carries the
    // draining recording's name, and degrades cleanly when the name is absent.
    func testTranslateFinalizeInProgressIsDistinctFromLockContended() {
        let service = LiveDaemonSessionService()
        let body = #"{"ok":false,"error":"finalize_in_progress","recording_name":"yesterday","retryable":true}"#
        let error = DaemonClientError.envelopeError(
            code: DaemonErrorCode.finalizeInProgress,
            rawBody: Data(body.utf8)
        )

        XCTAssertEqual(service.translateFailure(error), .finalizeInProgress(recordingName: "yesterday"))
        XCTAssertNotEqual(service.translateFailure(error), .lockContended)

        // Name omitted (daemon did not know it) still resolves, not throws.
        let unnamed = DaemonClientError.envelopeError(
            code: DaemonErrorCode.finalizeInProgress,
            rawBody: Data(#"{"ok":false,"error":"finalize_in_progress"}"#.utf8)
        )
        XCTAssertEqual(service.translateFailure(unnamed), .finalizeInProgress(recordingName: nil))
    }

    // SCR-276: the copy is the whole point of the ticket — assert it says what
    // is happening and names the retry, and never claims a recording is active.
    func testFinalizeInProgressCopyDoesNotClaimARecordingIsActive() {
        let named = RecorderController.finalizeInProgressErrorMessage(recordingName: "yesterday")
        XCTAssertTrue(named.contains("yesterday"))
        XCTAssertTrue(named.contains("still finishing processing"))
        XCTAssertTrue(named.contains("Try again in a moment"))
        XCTAssertFalse(named.contains("already recording"))

        let unnamed = RecorderController.finalizeInProgressErrorMessage(recordingName: nil)
        XCTAssertTrue(unnamed.hasPrefix("The previous recording"))
        XCTAssertFalse(unnamed.contains("already recording"))
    }

    func testTranslatePermissionRequiredDecodesMissingList() {
        let service = LiveDaemonSessionService()
        let body = #"{"ok":false,"error":"permission_required","missing":["screen_recording","accessibility"]}"#
        let error = DaemonClientError.envelopeError(
            code: DaemonErrorCode.permissionRequired,
            rawBody: Data(body.utf8)
        )

        XCTAssertEqual(
            service.translateFailure(error),
            .permissionRequired(missing: ["screen_recording", "accessibility"])
        )
    }

    func testTranslatePermissionRequiredWithUndecodableBodyIsEmptyMissing() {
        let service = LiveDaemonSessionService()
        let error = DaemonClientError.envelopeError(
            code: DaemonErrorCode.permissionRequired,
            rawBody: Data()
        )

        XCTAssertEqual(service.translateFailure(error), .permissionRequired(missing: []))
    }

    func testTranslateOtherEnvelopeErrorCarriesLocalizedDescription() {
        let service = LiveDaemonSessionService()
        let error = DaemonClientError.envelopeError(code: "unexpected_code", rawBody: Data())

        guard case .other(let description) = service.translateFailure(error) else {
            return XCTFail("Expected .other for unrecognized envelope code")
        }
        XCTAssertEqual(description, error.localizedDescription)
    }

    func testTranslateUnknownErrorFallsThroughToOther() {
        let service = LiveDaemonSessionService()
        struct CustomError: Error, LocalizedError {
            var errorDescription: String? { "custom failure" }
        }

        guard case .other(let description) = service.translateFailure(CustomError()) else {
            return XCTFail("Expected .other for unknown error type")
        }
        XCTAssertEqual(description, "custom failure")
    }
}
