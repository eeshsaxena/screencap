import XCTest
@testable import ScreenCap

/// Focused tests for `DaemonSessionService.translateFailure(_:)`. The
/// transport-level behaviors (probe outcomes, snapshot outcomes, event
/// stream reconnect/backoff, cursor recovery) are exercised end-to-end by
/// `RecorderControllerDaemonTests` via `UnixHTTPTestServer`.
@MainActor
final class DaemonSessionServiceTests: XCTestCase {
    func testTranslateSchemaMismatch() {
        let service = DaemonSessionService()

        let outcome = service.translateFailure(DaemonClientError.schemaMismatch(expected: 1, got: 99))

        XCTAssertEqual(outcome, .schemaMismatch)
    }

    func testTranslateSocketUnavailable() {
        let service = DaemonSessionService()

        let outcome = service.translateFailure(DaemonClientError.socketUnavailable(path: "/tmp/x"))

        XCTAssertEqual(outcome, .socketUnavailable)
    }

    func testTranslateConnectionFailed() {
        let service = DaemonSessionService()
        struct Boom: Error {}

        let outcome = service.translateFailure(DaemonClientError.connectionFailed(underlying: Boom()))

        XCTAssertEqual(outcome, .socketUnavailable)
    }

    func testTranslateLockContendedEnvelopeError() {
        let service = DaemonSessionService()
        let error = DaemonClientError.envelopeError(code: DaemonErrorCode.lockContended, rawBody: Data())

        XCTAssertEqual(service.translateFailure(error), .lockContended)
    }

    func testTranslateNotOwnedByDaemonEnvelopeError() {
        let service = DaemonSessionService()
        let error = DaemonClientError.envelopeError(code: DaemonErrorCode.notOwnedByDaemon, rawBody: Data())

        XCTAssertEqual(service.translateFailure(error), .lockContended)
    }

    func testTranslateOtherEnvelopeErrorCarriesLocalizedDescription() {
        let service = DaemonSessionService()
        let error = DaemonClientError.envelopeError(code: "unexpected_code", rawBody: Data())

        guard case .other(let description) = service.translateFailure(error) else {
            return XCTFail("Expected .other for unrecognized envelope code")
        }
        XCTAssertEqual(description, error.localizedDescription)
    }

    func testTranslateUnknownErrorFallsThroughToOther() {
        let service = DaemonSessionService()
        struct CustomError: Error, LocalizedError {
            var errorDescription: String? { "custom failure" }
        }

        guard case .other(let description) = service.translateFailure(CustomError()) else {
            return XCTFail("Expected .other for unknown error type")
        }
        XCTAssertEqual(description, "custom failure")
    }
}
