import XCTest
@testable import Screencap

/// Coverage for `CLIClient`'s non-zero-exit handling — the seam behind the
/// "screencap exited with code 1:" empty-message subscribe bug.
///
/// `checkout-url` (and any `--json` command that reports failure as a
/// `{ok:false, error}` stdout envelope followed by `sys.exit(1)` with *empty*
/// stderr) needs `runJSONRaw(allowNonZeroExit: true)` so the envelope survives
/// the non-zero exit and the caller can surface the real reason instead of a
/// bare "exited with code 1:". The existing `FakeCloudAuthService` mock sits
/// *above* this seam — it hands back `Data` directly — which is exactly why the
/// bug slipped through. These tests drive the real spawn path against a fake
/// `screencap` injected via `SCREENCAP_CLI_PATH` (`FakeCLIBinary`), so they stay
/// Python-free.
final class CLIClientTests: XCTestCase {
    private var fakeCLI: FakeCLIBinary?

    override func tearDown() {
        fakeCLI?.remove()
        fakeCLI = nil
        super.tearDown()
    }

    /// The fix: a non-zero exit whose failure detail is a `{ok:false, error}`
    /// stdout envelope is *returned* (not thrown), so the caller can decode the
    /// real reason. This is what lets `startCheckout` show "Sign in to
    /// upgrade…" / "Checkout service unavailable…" instead of a bare exit code.
    func testRunJSONRawAllowNonZeroExitReturnsStdoutEnvelope() async throws {
        fakeCLI = try FakeCLIBinary(
            stdout: #"{"ok": false, "error": "boom from fake CLI"}"#, exitCode: 1
        )

        let data = try await CLIClient.runJSONRaw(
            ["checkout-url", "--json"], allowNonZeroExit: true
        )

        struct Envelope: Decodable { let ok: Bool; let error: String }
        let envelope = try JSONDecoder().decode(Envelope.self, from: data)
        XCTAssertFalse(envelope.ok)
        XCTAssertEqual(envelope.error, "boom from fake CLI")
    }

    /// Regression guard for the ~15 other `runJSONRaw` callers: the default
    /// still throws `nonZeroExit` on a non-zero exit, carrying the *empty*
    /// stderr. This is the exact behavior that produced the bare
    /// "exited with code 1:" for checkout before it opted into the tolerant path.
    func testRunJSONRawDefaultThrowsWithEmptyStderrOnNonZeroExit() async throws {
        fakeCLI = try FakeCLIBinary(
            stdout: #"{"ok": false, "error": "boom from fake CLI"}"#, exitCode: 1
        )

        do {
            _ = try await CLIClient.runJSONRaw(["checkout-url", "--json"])
            XCTFail("default runJSONRaw should throw on a non-zero exit")
        } catch let CLIError.nonZeroExit(code, stderr) {
            XCTAssertEqual(code, 1)
            XCTAssertEqual(
                stderr, "",
                "stderr is empty — the real error rode on stdout, which the throw discards"
            )
        }
    }
}
