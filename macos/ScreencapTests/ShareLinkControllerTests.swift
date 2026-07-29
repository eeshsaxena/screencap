import Foundation
import XCTest
@testable import Screencap

/// SCR-299 U4 — create/revoke/refresh orchestration over a fake daemon. The
/// pasteboard is injected, so these never touch the developer's real clipboard.
@MainActor
final class ShareLinkControllerTests: XCTestCase {

    // MARK: - Copy (R1, AE5)

    func testSuccessfulCreatePutsFragmentBearingLinkOnPasteboard() async {
        let service = FakeShareLinkService()
        service.createResult = .success(
            makeResponse(url: "https://screencap.sh/share/tok9#Zm9va2V5", token: "tok9")
        )
        let pasteboard = PasteboardSpy()
        let controller = makeController(service: service, pasteboard: pasteboard)

        await controller.copyLink()

        XCTAssertEqual(controller.phase, .hasLink(token: "tok9"))
        XCTAssertEqual(pasteboard.written, ["https://screencap.sh/share/tok9#Zm9va2V5"])
        // The fragment IS the key — a link copied without it is useless.
        XCTAssertTrue(pasteboard.written.first?.contains("#") == true)
        XCTAssertEqual(controller.lastCreatedURL?.fragment, "Zm9va2V5")
    }

    /// After a create the menu must offer revoke without needing a reopen.
    func testCreateLeavesActiveTokenAvailableForRevoke() async {
        let service = FakeShareLinkService()
        service.createResult = .success(makeResponse(url: "https://s.sh/share/t#k", token: "t"))
        let controller = makeController(service: service)

        await controller.copyLink()

        XCTAssertEqual(controller.activeToken, "t")
    }

    func testCopyingAgainReusesTheLinkWithoutAnotherCreate() async {
        let service = FakeShareLinkService()
        service.createResult = .success(makeResponse(url: "https://s.sh/share/t#k", token: "t"))
        let pasteboard = PasteboardSpy()
        let controller = makeController(service: service, pasteboard: pasteboard)

        await controller.copyLink()
        controller.copyExistingLink()

        XCTAssertEqual(service.createCallCount, 1)
        XCTAssertEqual(pasteboard.written.count, 2)
    }

    // MARK: - Revoke (R4, AE6)

    func testRevokeReturnsToNoLinkAndStopsOfferingRevoke() async {
        let service = FakeShareLinkService()
        service.createResult = .success(makeResponse(url: "https://s.sh/share/t#k", token: "t"))
        service.revokeResult = .success(makeResponse())
        let controller = makeController(service: service)

        await controller.copyLink()
        await controller.revoke()

        XCTAssertEqual(controller.phase, .noLink)
        XCTAssertNil(controller.activeToken)
        XCTAssertEqual(service.revokedTokens, ["t"])
        XCTAssertNil(controller.lastCreatedURL)
    }

    func testRevokeWithNoActiveLinkIsANoOp() async {
        let service = FakeShareLinkService()
        let controller = makeController(service: service)

        await controller.revoke()

        XCTAssertTrue(service.revokedTokens.isEmpty)
        XCTAssertEqual(controller.phase, .noLink)
    }

    // MARK: - Refresh (R14)

    func testRefreshFindsAnExistingLiveLinkForThisRecording() async {
        let service = FakeShareLinkService()
        service.listResult = .success([
            record(token: "other", recording: "rec-OTHER"),
            record(token: "mine", recording: "rec-1"),
        ])
        let controller = makeController(service: service)

        await controller.refresh()

        XCTAssertEqual(controller.phase, .hasLink(token: "mine"))
    }

    /// A list failure is not something the user asked for, so it must not
    /// present as an error banner over an otherwise working menu.
    func testRefreshFailureLeavesStateUntouched() async {
        let service = FakeShareLinkService()
        service.listResult = .failure(DaemonClientError.socketUnavailable(path: "/tmp/x"))
        let controller = makeController(service: service)

        await controller.refresh()

        XCTAssertEqual(controller.phase, .noLink)
    }

    // MARK: - Failures (R13, AE7)

    func testSignedOutCreateSurfacesSignInCopyAndCopiesNothing() async {
        let service = FakeShareLinkService()
        service.createResult = .failure(
            DaemonClientError.envelopeError(code: "not_signed_in", rawBody: Data())
        )
        let pasteboard = PasteboardSpy()
        let controller = makeController(service: service, pasteboard: pasteboard)

        await controller.copyLink()

        XCTAssertEqual(controller.phase, .failed(.notSignedIn))
        XCTAssertEqual(ShareLinkErrorCopy.notSignedIn.action, .signIn)
        XCTAssertTrue(pasteboard.written.isEmpty)
        XCTAssertNil(controller.lastCreatedURL)
    }

    func testTransientFailureOffersRetryAndASubsequentRetrySucceeds() async {
        let service = FakeShareLinkService()
        service.createResult = .failure(
            DaemonClientError.envelopeError(code: "share_backend_unavailable", rawBody: Data())
        )
        let controller = makeController(service: service)

        await controller.copyLink()
        XCTAssertEqual(controller.phase, .failed(.backendUnavailable))
        XCTAssertEqual(ShareLinkErrorCopy.backendUnavailable.action, .retry)

        service.createResult = .success(makeResponse(url: "https://s.sh/share/t#k", token: "t"))
        await controller.copyLink()

        XCTAssertEqual(controller.phase, .hasLink(token: "t"))
    }

    /// A create that returns a malformed link must fail rather than copying
    /// garbage the recipient cannot open.
    func testMalformedLinkFailsWithoutCopying() async {
        let service = FakeShareLinkService()
        service.createResult = .success(makeResponse(url: nil, token: "t"))
        let pasteboard = PasteboardSpy()
        let controller = makeController(service: service, pasteboard: pasteboard)

        await controller.copyLink()

        XCTAssertEqual(controller.phase, .failed(.unknown))
        XCTAssertTrue(pasteboard.written.isEmpty)
    }

    /// A create can run for minutes, so a second tap must not fire a second
    /// call — the window would otherwise mint two shares from one intent.
    func testConcurrentCopyIsANoOpWhileACreateIsInFlight() async {
        let service = FakeShareLinkService()
        service.createResult = .success(makeResponse(url: "https://s.sh/share/t#k", token: "t"))
        service.beforeCreate = { @MainActor controller in
            // Re-enter while the first call is still in flight.
            await controller.copyLink()
        }
        let controller = makeController(service: service)
        service.reentrantTarget = controller

        await controller.copyLink()

        XCTAssertEqual(service.createCallCount, 1)
    }

    // MARK: - Fixtures

    private func makeController(
        service: ShareLinkService,
        pasteboard: PasteboardSpy = PasteboardSpy()
    ) -> ShareLinkController {
        ShareLinkController(
            recordingName: "rec-1",
            service: service,
            writeToPasteboard: { pasteboard.written.append($0) }
        )
    }

    private func makeResponse(
        url: String? = nil,
        token: String? = nil
    ) -> RecordingShareResponse {
        var payload: [String: Any] = [:]
        if let url { payload["url"] = url }
        if let token { payload["token"] = token }
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return try! JSONDecoder().decode(RecordingShareResponse.self, from: data)
    }

    private func record(token: String, recording: String) -> ShareRecord {
        let payload: [String: Any] = [
            "token": token,
            "recording": recording,
            "expires_at": "2099-01-01T00:00:00+00:00",
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return try! JSONDecoder().decode(ShareRecord.self, from: data)
    }
}

/// Records what would have gone to the system pasteboard.
@MainActor
final class PasteboardSpy {
    var written: [String] = []
}

@MainActor
final class FakeShareLinkService: ShareLinkService {
    var createResult: Result<RecordingShareResponse, Error> = .failure(
        DaemonClientError.socketUnavailable(path: "/tmp/unset")
    )
    var revokeResult: Result<RecordingShareResponse, Error> = .failure(
        DaemonClientError.socketUnavailable(path: "/tmp/unset")
    )
    var listResult: Result<[ShareRecord], Error> = .success([])

    private(set) var createCallCount = 0
    private(set) var revokedTokens: [String] = []

    /// Hook used to prove the re-entry guard: runs inside the first create.
    var beforeCreate: (@MainActor (ShareLinkController) async -> Void)?
    weak var reentrantTarget: ShareLinkController?

    func create(recording: String) async throws -> RecordingShareResponse {
        createCallCount += 1
        if let beforeCreate, let reentrantTarget {
            self.beforeCreate = nil  // fire once
            await beforeCreate(reentrantTarget)
        }
        return try createResult.get()
    }

    func revoke(token: String) async throws -> RecordingShareResponse {
        revokedTokens.append(token)
        return try revokeResult.get()
    }

    func list() async throws -> [ShareRecord] {
        try listResult.get()
    }
}
