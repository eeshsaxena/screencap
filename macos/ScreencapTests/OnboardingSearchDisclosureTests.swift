import XCTest
@testable import Screencap

/// Search U8 (R6): the on-by-default disclosure's decision + copy + actions, driven
/// with an injected CLI seam (no real `screencap` process).
@MainActor
final class OnboardingSearchDisclosureTests: XCTestCase {

    // MARK: - Presentation decision

    func testPresentsOnlyWhenNeitherAcknowledgedNorDeclined() {
        XCTAssertTrue(SearchDisclosurePolicy.shouldPresent(acknowledged: false, declined: false))
        XCTAssertFalse(SearchDisclosurePolicy.shouldPresent(acknowledged: true, declined: false))
        XCTAssertFalse(SearchDisclosurePolicy.shouldPresent(acknowledged: false, declined: true))
    }

    // MARK: - Honesty-gated copy (R6 mechanism points + R1 caveat)

    func testCopyIsHonestAboutTheMechanism() {
        let points = SearchDisclosureCopy.points(retentionDays: 30).joined(separator: " ").lowercased()
        XCTAssertTrue(points.contains("encrypted"))
        XCTAssertTrue(points.contains("nothing about search is uploaded") || points.contains("nothing"))
        XCTAssertTrue(points.contains("password") || points.contains("secret"))
        XCTAssertTrue(points.contains("30 days"))
        XCTAssertTrue(points.contains("pause"))
        XCTAssertTrue(points.contains("exclude"))
        XCTAssertTrue(points.contains("turn search off") || points.contains("off"))
        XCTAssertTrue(SearchDisclosureCopy.limitNote.lowercased().contains("can't catch everything"))
    }

    // MARK: - Actions run the right CLI + settle the phase

    func testEnableRunsSearchEnable() async {
        var captured: [[String]] = []
        let controller = SearchDisclosureController(run: { captured.append($0) })
        await controller.enable()
        XCTAssertEqual(captured, [["search", "enable"]])
        XCTAssertEqual(controller.phase, .enabled)
    }

    func testDeclineWritesConsentDeclined() async {
        var captured: [[String]] = []
        let controller = SearchDisclosureController(run: { captured.append($0) })
        await controller.decline()
        XCTAssertEqual(captured, [["settings", "--set", "content_index_consent_declined=true"]])
        XCTAssertEqual(controller.phase, .declined)
    }

    func testEnableFailureSurfacesError() async {
        struct Boom: Error {}
        let controller = SearchDisclosureController(run: { _ in throw Boom() })
        await controller.enable()
        if case .failed = controller.phase {} else {
            XCTFail("expected .failed, got \(controller.phase)")
        }
    }

    // MARK: - A failed enable must name WHY (not just "please try again")

    /// A `search_enable_failed` event as the CLI emits it, wrapped in the surrounding
    /// human-readable output the app also receives.
    private func failureStderr(reason: String, detail: String) -> String {
        """
        Enabling on-device search — encrypting your existing recordings...
        {"type":"search_enable_failed","ts":1.0,"schema_version":1,\
        "reason":"\(reason)","detail":"\(detail)"}
        Could not enable search (\(reason)): \(detail)
        """
    }

    /// The regression: `search enable` reports its reason on stderr, and the sheet has
    /// to carry it through. A fixed string leaves a tester with nothing to report.
    func testEnableFailureCarriesTheCLIReasonIntoTheMessage() async {
        let stderr = failureStderr(
            reason: "corpus_key_unavailable",
            detail: "could not store the corpus key in the shared Keychain group (status -25308)"
        )
        let controller = SearchDisclosureController(
            run: { _ in throw CLIError.nonZeroExit(code: 1, stderr: stderr) }
        )
        await controller.enable()

        guard case .failed(let message) = controller.phase else {
            return XCTFail("expected .failed, got \(controller.phase)")
        }
        XCTAssertTrue(message.lowercased().contains("keychain"), message)
        // The raw detail survives so a tester can report the actual status code.
        XCTAssertTrue(message.contains("-25308"), message)
        // A Keychain refusal is deterministic — never invite a pointless retry.
        XCTAssertFalse(message.lowercased().contains("please try again"), message)
    }

    /// Decodes the stderr shape a real `screencap search enable` failure produced when
    /// captured: `emit_event`'s spacing, a float `ts`, and the rich-wrapped human lines
    /// after it. This pins the format **as observed at capture time** — it does not
    /// re-derive it from the live Python emitter, so a producer-side change would be
    /// caught by `tests/test_search_cli_e2e.py`'s event assertions, not by this test.
    ///
    /// The fixture is deliberately the KEY-FILE channel, not Keychain: all three
    /// `_persist_key` channels report `corpus_key_unavailable`, so the headline must
    /// not name Keychain (see `headline(for:)`).
    func testDecodesTheRealCLIStderrVerbatim() {
        let stderr = """
        {"type": "search_enable_failed", "ts": 1785241835.678283, "schema_version": 1, \
        "reason": "corpus_key_unavailable", "detail": "could not write the corpus key file \
        '/tmp/lk/corpus.key': [Errno 13] Permission denied"}
        Could not enable search (corpus_key_unavailable): could not write the corpus key
        file
        '/tmp/lk/corpus.key': [Errno 13] Permission denied
        """
        let failure = SearchEnableFailure.classify(CLIError.nonZeroExit(code: 1, stderr: stderr))
        XCTAssertEqual(failure.reasonCode, "corpus_key_unavailable")
        XCTAssertTrue(failure.message.contains("Errno 13"), failure.message)
        // A key-FILE permission error must not be reported as a Keychain problem.
        XCTAssertFalse(failure.message.lowercased().contains("keychain"), failure.message)
        XCTAssertTrue(failure.message.lowercased().contains("securely store"), failure.message)
    }

    /// The helper-unavailable arm (`.binaryNotFound` / `.launchFailed`) — a real
    /// production path when the bundled CLI is missing or can't be spawned.
    func testHelperUnavailableNamesTheHelperAndKeepsTheDetail() {
        let failure = SearchEnableFailure.classify(
            CLIError.binaryNotFound(searchedPaths: ["/Applications/Screencap.app/x"])
        )
        XCTAssertEqual(failure.reasonCode, "binary_not_found")
        XCTAssertTrue(failure.message.lowercased().contains("helper"), failure.message)
        XCTAssertTrue(failure.message.contains("/Applications/Screencap.app/x"), failure.message)

        struct Underlying: Error {}
        XCTAssertEqual(
            SearchEnableFailure.classify(CLIError.launchFailed(underlying: Underlying())).reasonCode,
            "launch_failed"
        )
    }

    /// A non-`CLIError` still produces a message, not just a `.failed` phase.
    func testNonCLIErrorStillProducesAMessage() {
        struct Boom: LocalizedError { var errorDescription: String? { "the boom detail" } }
        let failure = SearchEnableFailure.classify(Boom())
        XCTAssertEqual(failure.reasonCode, "unknown")
        XCTAssertTrue(failure.message.contains("the boom detail"), failure.message)
    }

    /// A decodable event missing `reason` falls back rather than producing empty copy.
    func testEventWithoutAReasonFallsBack() {
        let stderr = #"{"type":"search_enable_failed","schema_version":1,"detail":"d"}"#
        let failure = SearchEnableFailure.classify(CLIError.nonZeroExit(code: 1, stderr: stderr))
        XCTAssertEqual(failure.reasonCode, "cli_exit")
        XCTAssertTrue(failure.message.contains("Couldn't turn on search"), failure.message)
    }

    /// The logged reason code is `privacy: .public`, so a producer that put free text
    /// (a path, a swapped argument) in `reason` must not reach the unified log.
    func testFreeTextReasonIsNotLoggedVerbatim() {
        let stderr = #"{"type":"search_enable_failed","reason":"/Users/me/rec ABC","detail":"d"}"#
        let failure = SearchEnableFailure.classify(CLIError.nonZeroExit(code: 1, stderr: stderr))
        XCTAssertEqual(failure.reasonCode, "unrecognized")
        XCTAssertFalse(failure.reasonCode.contains("/Users"))
    }

    /// An unbounded detail must not grow the sheet until its buttons leave the screen.
    func testOverlongDetailIsBounded() {
        let stderr = failureStderr(reason: "migration_incomplete", detail: String(repeating: "x", count: 5000))
        let message = SearchEnableFailure.message(for: CLIError.nonZeroExit(code: 1, stderr: stderr))
        XCTAssertLessThan(message.count, 1000, "message should be bounded, was \(message.count)")
        XCTAssertTrue(message.hasSuffix("…"), "expected truncation marker")
    }

    /// An incomplete migration IS resumable, so this one does invite a retry.
    func testIncompleteMigrationInvitesARetry() {
        let stderr = failureStderr(
            reason: "migration_incomplete",
            detail: "some plaintext stills or a plaintext index survived the pass"
        )
        let message = SearchEnableFailure.message(
            for: CLIError.nonZeroExit(code: 1, stderr: stderr)
        )
        XCTAssertTrue(message.lowercased().contains("trying again"), message)
        XCTAssertTrue(message.lowercased().contains("couldn't be encrypted"), message)
    }

    /// A timeout is its own explanation — the flip encrypts the existing corpus first.
    func testTimeoutSaysItTimedOutAndIsResumable() {
        let message = SearchEnableFailure.message(for: CLIError.timedOut(seconds: 300))
        XCTAssertTrue(message.contains("5 minutes"), message)
        XCTAssertTrue(message.lowercased().contains("pick up where it left off"), message)
        // Sub-5-minute timeouts must not read "1 minutes" / "0 minutes".
        XCTAssertTrue(SearchEnableFailure.message(for: CLIError.timedOut(seconds: 60))
            .contains("1 minute and"), "singular minute")
        XCTAssertTrue(SearchEnableFailure.message(for: CLIError.timedOut(seconds: 45))
            .contains("45 seconds"), "sub-minute")
    }

    /// Non-zero exit with no decodable event still beats a bare retry prompt.
    func testUnparseableFailureStillNamesTheExitCode() {
        let message = SearchEnableFailure.message(
            for: CLIError.nonZeroExit(code: 137, stderr: "Killed: 9")
        )
        XCTAssertTrue(message.contains("137"), message)
    }

    /// A future Python-side reason the app doesn't recognize must degrade to the
    /// generic headline carrying the detail — never crash or drop the reason.
    func testUnrecognizedReasonStillSurfacesTheDetail() {
        let stderr = failureStderr(reason: "some_future_reason", detail: "the specifics")
        let failure = SearchEnableFailure.classify(CLIError.nonZeroExit(code: 1, stderr: stderr))
        XCTAssertEqual(failure.reasonCode, "some_future_reason")
        XCTAssertTrue(failure.message.contains("the specifics"), failure.message)
    }

    /// The reason code is what gets logged — it must never be the raw detail, which
    /// can name the user's recordings.
    func testReasonCodeIsTheShortCodeNotTheDetail() {
        let stderr = failureStderr(
            reason: "migration_incomplete", detail: "could not encrypt my-secret-recording"
        )
        let code = SearchEnableFailure.reasonCode(
            for: CLIError.nonZeroExit(code: 1, stderr: stderr)
        )
        XCTAssertEqual(code, "migration_incomplete")
        XCTAssertFalse(code.contains("my-secret-recording"))
        XCTAssertEqual(SearchEnableFailure.reasonCode(for: CLIError.timedOut(seconds: 300)), "timed_out")
    }
}
