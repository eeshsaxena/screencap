import UniformTypeIdentifiers
import XCTest
@testable import Screencap

/// U3: controller lifecycle, prefill, selection validation, per-kind error
/// mapping, timeout scaling, and the U4 form-policy derivations.
@MainActor
final class FeedbackControllerTests: XCTestCase {
    private func makeController(
        service: FakeFeedbackService,
        daemonVersionFetch: @escaping @Sendable () async throws -> String = { "9.9.9" },
        daemonProbeTimeout: TimeInterval = 0.05
    ) -> FeedbackController {
        FeedbackController(
            service: service,
            daemonVersionFetch: daemonVersionFetch,
            daemonProbeTimeout: daemonProbeTimeout,
            appVersion: "1.2.3",
            macOSVersion: "15.5.0"
        )
    }

    /// Poll until `condition` holds (or the timeout passes) — the controller's
    /// send / daemon-probe Tasks have no completion to await directly. Mirrors
    /// `CloudAuthControllerTests.waitUntil` / `RecorderControllerTests.waitUntil`.
    private func waitUntil(
        _ message: String,
        timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: @escaping @MainActor () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            await Task.yield()
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail(message, file: file, line: line)
    }

    private func waitUntilSettled(
        _ controller: FeedbackController,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        await waitUntil("send never settled", file: file, line: line) {
            controller.state != .sending
        }
    }

    private func waitForDaemonVersion(
        _ controller: FeedbackController,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        await waitUntil("daemon probe never resolved", file: file, line: line) {
            controller.daemonVersion != nil
        }
    }

    private func makeTempFile(name: String, bytes: Int) throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(name)
        try Data(count: bytes).write(to: url)
        return url
    }

    // MARK: - Email prefill (AE1/AE5)

    func testSignedInEmailPrefills() {
        let controller = makeController(service: FakeFeedbackService())
        controller.prepareForPresentation(
            auth: .signedIn(email: "user@example.com", uid: "u1", stale: false)
        )
        XCTAssertEqual(controller.email, "user@example.com")
    }

    func testStaleNilEmailYieldsEmptyField() {
        let controller = makeController(service: FakeFeedbackService())
        controller.prepareForPresentation(
            auth: .signedIn(email: nil, uid: nil, stale: true)
        )
        XCTAssertEqual(controller.email, "")
    }

    func testSignedOutYieldsEmptyField() {
        let controller = makeController(service: FakeFeedbackService())
        controller.prepareForPresentation(auth: .signedOut)
        XCTAssertEqual(controller.email, "")
    }

    func testClearedEmailStaysClearedOnReopen() {
        let controller = makeController(service: FakeFeedbackService())
        controller.prepareForPresentation(
            auth: .signedIn(email: "user@example.com", uid: "u1", stale: false)
        )
        controller.email = ""
        controller.prepareForPresentation(
            auth: .signedIn(email: "user@example.com", uid: "u1", stale: false)
        )
        XCTAssertEqual(controller.email, "", "prefill is a one-shot per draft")
    }

    func testClearedEmailSendsNoIdentity() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.prepareForPresentation(
            auth: .signedIn(email: "user@example.com", uid: "u1", stale: false)
        )
        controller.email = ""
        controller.message = "it broke"
        controller.send()
        await waitUntilSettled(controller)
        XCTAssertEqual(service.lastPayloadJSON["email"] as? String, "")
    }

    // MARK: - Selection-time validation (AE2/AE6/AE7)

    func testOversizeFileRejectedWithFileReason() {
        let rejection = FeedbackAttachmentPolicy.rejection(
            fileName: "clip.mov", fileExtension: "mov",
            sizeBytes: FeedbackCaps.maxFileBytes + 1,
            existingCount: 0, existingTotalBytes: 0
        )
        XCTAssertEqual(rejection, .fileTooLarge(fileName: "clip.mov"))
    }

    func testTotalCapBreachOnNthFile() {
        let rejection = FeedbackAttachmentPolicy.rejection(
            fileName: "third.mov", fileExtension: "mov",
            sizeBytes: 20 * 1024 * 1024,
            existingCount: 2,
            existingTotalBytes: 45 * 1024 * 1024
        )
        XCTAssertEqual(rejection, .totalTooLarge(fileName: "third.mov"))
    }

    func testCountCapBreach() {
        let rejection = FeedbackAttachmentPolicy.rejection(
            fileName: "sixth.png", fileExtension: "png",
            sizeBytes: 10,
            existingCount: FeedbackCaps.maxAttachments,
            existingTotalBytes: 50
        )
        XCTAssertEqual(rejection, .tooMany)
    }

    func testLogFileRejectedAsUnsupportedType() {
        // Type check outranks size: an oversize .log reads "unsupported", not
        // "too large" — the honest reason the user can act on.
        let rejection = FeedbackAttachmentPolicy.rejection(
            fileName: "daemon.log", fileExtension: "log",
            sizeBytes: FeedbackCaps.maxFileBytes + 1,
            existingCount: 0, existingTotalBytes: 0
        )
        XCTAssertEqual(rejection, .unsupportedType(fileName: "daemon.log"))
    }

    func testValidFilePassesAndMapsContentType() {
        XCTAssertNil(FeedbackAttachmentPolicy.rejection(
            fileName: "shot.PNG", fileExtension: "PNG",
            sizeBytes: 1024, existingCount: 0, existingTotalBytes: 0
        ))
        XCTAssertEqual(FeedbackAttachmentPolicy.contentType(forExtension: "PNG"), "image/png")
        XCTAssertEqual(FeedbackAttachmentPolicy.contentType(forExtension: "mov"), "video/quicktime")
        XCTAssertNil(FeedbackAttachmentPolicy.contentType(forExtension: "log"))
    }

    func testRejectionMessagesAreDistinct() {
        let rejections: [FeedbackAttachmentRejection] = [
            .unsupportedType(fileName: "f"),
            .fileTooLarge(fileName: "f"),
            .totalTooLarge(fileName: "f"),
            .tooMany,
            .unreadable(fileName: "f"),
        ]
        let messages = rejections.map(\.message)
        XCTAssertEqual(Set(messages).count, messages.count)
    }

    // MARK: - Attachment add/remove

    func testAddThenRemoveReturnsToTextOnly() throws {
        let controller = makeController(service: FakeFeedbackService())
        let url = try makeTempFile(name: "shot.png", bytes: 128)
        controller.addAttachments(urls: [url])
        XCTAssertEqual(controller.attachments.count, 1)
        XCTAssertNil(controller.attachmentRejection)
        XCTAssertEqual(controller.attachments[0].contentType, "image/png")
        XCTAssertEqual(controller.attachments[0].sizeBytes, 128)

        controller.removeAttachment(id: controller.attachments[0].id)
        XCTAssertTrue(controller.attachments.isEmpty)
        controller.message = "still sendable"
        XCTAssertTrue(controller.canSend)
    }

    func testUnsupportedPickSurfacesRejectionButKeepsValidPicks() throws {
        let controller = makeController(service: FakeFeedbackService())
        let good = try makeTempFile(name: "shot.png", bytes: 64)
        let bad = try makeTempFile(name: "daemon.log", bytes: 64)
        controller.addAttachments(urls: [bad, good])
        XCTAssertEqual(controller.attachmentRejection, .unsupportedType(fileName: "daemon.log"))
        XCTAssertEqual(controller.attachments.map(\.fileName), ["shot.png"])
    }

    func testMissingFileRejectedAsUnreadable() {
        let controller = makeController(service: FakeFeedbackService())
        let missing = URL(fileURLWithPath: "/nonexistent/shot.png")
        controller.addAttachments(urls: [missing])
        XCTAssertEqual(controller.attachmentRejection, .unreadable(fileName: "shot.png"))
        XCTAssertTrue(controller.attachments.isEmpty)
    }

    // MARK: - Daemon metadata (AE4 / KTD-9)

    func testDaemonProbeResolvesVersion() async {
        let controller = makeController(service: FakeFeedbackService())
        controller.prepareForPresentation(auth: .signedOut)
        await waitForDaemonVersion(controller)
        XCTAssertEqual(controller.daemonVersion, "9.9.9")
    }

    func testHangingDaemonYieldsUnavailableAndNeverBlocksOpen() async {
        let controller = makeController(
            service: FakeFeedbackService(),
            daemonVersionFetch: {
                // A stale-but-listening daemon: accepts, then hangs.
                try await Task.sleep(nanoseconds: 3_600 * 1_000_000_000)
                return "never"
            },
            daemonProbeTimeout: 0.05
        )
        // Open is synchronous — a hanging probe cannot block it.
        controller.prepareForPresentation(auth: .signedOut)
        await waitForDaemonVersion(controller)
        XCTAssertEqual(controller.daemonVersion, FeedbackController.daemonVersionUnavailable)
    }

    func testFailingDaemonYieldsUnavailableAndSubmissionProceeds() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(
            service: service,
            daemonVersionFetch: { throw CLIError.timedOut(seconds: 1) }
        )
        controller.prepareForPresentation(auth: .signedOut)
        await waitForDaemonVersion(controller)
        controller.message = "daemon is broken"
        controller.send()
        await waitUntilSettled(controller)
        let versions = service.lastPayloadJSON["versions"] as? [String: Any]
        XCTAssertEqual(versions?["daemon"] as? String, "unavailable")
        XCTAssertEqual(versions?["app"] as? String, "1.2.3")
        XCTAssertEqual(controller.state, .success(issueURL: nil))
    }

    // MARK: - Envelope mapping (AE3/AE8 / KTD-7)

    func testEachErrorKindMapsToDistinctFailureAndPreservesDraft() async {
        let cases: [(String, FeedbackErrorKind)] = [
            ("network", .network),
            ("server", .server),
            ("rate_limited", .rateLimited),
            ("too_large", .tooLarge),
            ("invalid", .invalid),
            ("expired", .expired),
        ]
        for (code, expected) in cases {
            let service = FakeFeedbackService()
            service.result = FakeFeedbackService.envelope(
                #"{"ok": false, "error_kind": "\#(code)", "message": "m", "retryable": false}"#
            )
            let controller = makeController(service: service)
            controller.message = "draft text"
            controller.email = "user@example.com"
            controller.send()
            await waitUntilSettled(controller)
            guard case .failure(let failure) = controller.state else {
                XCTFail("expected failure for \(code)"); continue
            }
            XCTAssertEqual(failure.kind, expected)
            XCTAssertEqual(failure.message, expected.userMessage)
            // Draft intact after failure.
            XCTAssertEqual(controller.message, "draft text")
            XCTAssertEqual(controller.email, "user@example.com")
        }
        // R9: no two kinds share copy — never one generic "couldn't send".
        let allCopy = FeedbackErrorKind.allCases.map(\.userMessage)
        XCTAssertEqual(Set(allCopy).count, allCopy.count)
    }

    func testInvalidCarriesRelayDetailOtherKindsDoNot() async {
        for (code, expectsDetail) in [("invalid", true), ("server", false)] {
            let service = FakeFeedbackService()
            service.result = FakeFeedbackService.envelope(
                #"{"ok": false, "error_kind": "\#(code)", "message": "message is too long", "retryable": false}"#
            )
            let controller = makeController(service: service)
            controller.message = "hi"
            controller.send()
            await waitUntilSettled(controller)
            guard case .failure(let failure) = controller.state else {
                XCTFail("expected failure"); continue
            }
            XCTAssertEqual(failure.detail, expectsDetail ? "message is too long" : nil)
        }
    }

    func testUnknownKindFallsBackToServer() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(
            #"{"ok": false, "error_kind": "brand_new_kind"}"#
        )
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        guard case .failure(let failure) = controller.state else {
            return XCTFail("expected failure")
        }
        XCTAssertEqual(failure.kind, .server)
        XCTAssertTrue(failure.retryable, "server defaults retryable")
    }

    func testEnvelopeRetryableFieldOverridesKindDefault() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(
            #"{"ok": false, "error_kind": "network", "retryable": false}"#
        )
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        guard case .failure(let failure) = controller.state else {
            return XCTFail("expected failure")
        }
        XCTAssertFalse(failure.retryable, "explicit envelope retryable wins")
    }

    func testOkTrueWithNilOptionalsIsSuccess() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        XCTAssertEqual(controller.state, .success(issueURL: nil))
    }

    func testSuccessCarriesIssueURL() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(
            #"{"ok": true, "issue_url": "https://linear.app/x/issue/SCR-1"}"#
        )
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        XCTAssertEqual(
            controller.state, .success(issueURL: "https://linear.app/x/issue/SCR-1")
        )
    }

    func testGarbageStdoutMapsToRetryableServerFailure() async {
        let service = FakeFeedbackService()
        service.result = Data("Rich console banner\n".utf8)
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        guard case .failure(let failure) = controller.state else {
            return XCTFail("expected failure")
        }
        XCTAssertEqual(failure.kind, .server)
        XCTAssertTrue(failure.retryable)
    }

    // MARK: - Send lifecycle (KTD-12)

    func testSendTransitionsThroughSendingAndSuccessClearsDraft() async throws {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.email = "user@example.com"
        let url = try makeTempFile(name: "shot.png", bytes: 64)
        controller.addAttachments(urls: [url])
        controller.send()
        XCTAssertEqual(controller.state, .sending)
        await waitUntilSettled(controller)
        guard case .success = controller.state else {
            return XCTFail("expected success")
        }
        XCTAssertEqual(controller.message, "")
        XCTAssertEqual(controller.email, "")
        XCTAssertTrue(controller.attachments.isEmpty)
    }

    func testSpawnFailureMapsToRetryableServer() async {
        // The non-timeout thrown-error branch: spawn/binary/decode failures
        // all take the retryable server fallback, never a wedge or a leak of
        // localizedDescription.
        let service = FakeFeedbackService()
        service.error = CLIError.binaryNotFound(searchedPaths: [])
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        guard case .failure(let failure) = controller.state else {
            return XCTFail("expected failure")
        }
        XCTAssertEqual(failure.kind, .server)
        XCTAssertTrue(failure.retryable)
        XCTAssertEqual(controller.message, "hi", "draft preserved")
    }

    func testReopenAfterSuccessShowsFreshForm() async {
        // Dismissing the success screen without tapping Done must not strand
        // the next open on last report's success (prepareForPresentation's
        // stale-success reset).
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        guard case .success = controller.state else {
            return XCTFail("expected success")
        }
        controller.prepareForPresentation(auth: .signedOut)
        XCTAssertEqual(controller.state, .idle)
    }

    func testSubprocessTimeoutMapsToRetryableNetwork() async {
        let service = FakeFeedbackService()
        service.error = CLIError.timedOut(seconds: 120)
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        guard case .failure(let failure) = controller.state else {
            return XCTFail("expected failure")
        }
        XCTAssertEqual(failure.kind, .network)
        XCTAssertTrue(failure.retryable)
        XCTAssertEqual(controller.message, "hi", "draft preserved")
    }

    func testCancelSendReturnsToEditableDraft() async {
        let service = FakeFeedbackService()
        service.suspendUntilCancelled = true
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        XCTAssertEqual(controller.state, .sending)
        controller.cancelSend()
        XCTAssertEqual(controller.state, .idle)
        XCTAssertEqual(controller.message, "hi")
        // The cancelled task's error path must not clobber the restored draft.
        await Task.yield()
        XCTAssertEqual(controller.state, .idle)
    }

    func testTimeoutScalesWithDeclaredBytes() async throws {
        // Pure scaling: floor, linear region, cap.
        XCTAssertEqual(FeedbackSendTimeout.seconds(declaredBytes: 0), 120)
        XCTAssertEqual(
            FeedbackSendTimeout.seconds(declaredBytes: 10 * 1024 * 1024), 210
        )
        XCTAssertEqual(
            FeedbackSendTimeout.seconds(declaredBytes: 60 * 1024 * 1024), 600
        )

        // And the controller actually hands the scaled value to the service.
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.message = "hi"
        let url = try makeTempFile(name: "clip.mov", bytes: 2 * 1024 * 1024)
        controller.addAttachments(urls: [url])
        controller.send()
        await waitUntilSettled(controller)
        XCTAssertEqual(service.sentTimeouts, [120], "2 MB stays on the floor")
        XCTAssertGreaterThan(
            service.sentTimeouts[0], 10,
            "never the runJSONRawStdin default that SIGTERMs uploads"
        )
    }

    func testAcknowledgeSuccessReturnsToIdle() async {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.message = "hi"
        controller.send()
        await waitUntilSettled(controller)
        controller.acknowledgeSuccess()
        XCTAssertEqual(controller.state, .idle)
    }

    // MARK: - Form policy (U4 view derivations)

    func testCanSendRequiresNonEmptyMessage() {
        XCTAssertFalse(FeedbackFormPolicy.canSend(message: "", email: "", isSending: false))
        XCTAssertFalse(FeedbackFormPolicy.canSend(message: "  \n", email: "", isSending: false))
        XCTAssertTrue(FeedbackFormPolicy.canSend(message: "hi", email: "", isSending: false))
    }

    func testCanSendBlocksWhileSendingAndOverMessageCap() {
        XCTAssertFalse(FeedbackFormPolicy.canSend(message: "hi", email: "", isSending: true))
        let huge = String(repeating: "a", count: FeedbackCaps.maxMessageChars + 1)
        XCTAssertFalse(FeedbackFormPolicy.canSend(message: huge, email: "", isSending: false))
        let atCap = String(repeating: "a", count: FeedbackCaps.maxMessageChars)
        XCTAssertTrue(FeedbackFormPolicy.canSend(message: atCap, email: "", isSending: false))
    }

    func testMessageCapCountsUnicodeScalarsLikeTheRelay() {
        // The relay caps on Python len() (code points). A flag emoji is ONE
        // grapheme but TWO scalars — grapheme counting would pass the client
        // gate and bounce off the relay as `invalid`.
        let flags = String(repeating: "🇵🇹", count: FeedbackCaps.maxMessageChars / 2 + 1)
        XCTAssertLessThanOrEqual(flags.count, FeedbackCaps.maxMessageChars)
        XCTAssertTrue(FeedbackFormPolicy.isMessageOverCap(flags))
        XCTAssertFalse(FeedbackFormPolicy.canSend(message: flags, email: "", isSending: false))
    }

    func testPanelTypeFilterDerivesFromCaps() {
        // Every accepted extension must resolve to a UTType — one that
        // doesn't would silently vanish from the NSOpenPanel filter while the
        // validator still accepts it.
        for ext in FeedbackCaps.contentTypeByExtension.keys {
            XCTAssertNotNil(
                UTType(filenameExtension: ext),
                "extension \(ext) resolves to no UTType — invisible in the panel"
            )
        }
        XCTAssertFalse(FeedbackCaps.allowedUTTypes.isEmpty)
    }

    func testEmailAcceptance() {
        XCTAssertTrue(FeedbackFormPolicy.isEmailAcceptable(""))
        XCTAssertTrue(FeedbackFormPolicy.isEmailAcceptable("user@example.com"))
        XCTAssertFalse(FeedbackFormPolicy.isEmailAcceptable("not-an-email"))
        XCTAssertFalse(FeedbackFormPolicy.isEmailAcceptable("a@b"))
        XCTAssertFalse(
            FeedbackFormPolicy.canSend(message: "hi", email: "not-an-email", isSending: false)
        )
    }

    // MARK: - Payload contract

    func testPayloadCarriesTypeMessageAndAttachmentContentTypes() async throws {
        let service = FakeFeedbackService()
        service.result = FakeFeedbackService.envelope(#"{"ok": true}"#)
        let controller = makeController(service: service)
        controller.requestType = .feature
        controller.message = "please add X"
        let url = try makeTempFile(name: "demo.mov", bytes: 64)
        controller.addAttachments(urls: [url])
        controller.send()
        await waitUntilSettled(controller)

        let payload = service.lastPayloadJSON
        XCTAssertEqual(payload["type"] as? String, "feature")
        XCTAssertEqual(payload["message"] as? String, "please add X")
        let attachments = payload["attachments"] as? [[String: Any]]
        XCTAssertEqual(attachments?.count, 1)
        XCTAssertEqual(attachments?[0]["content_type"] as? String, "video/quicktime")
        XCTAssertEqual(attachments?[0]["path"] as? String, url.path)
    }
}
