import XCTest
@testable import ScreenCap

/// U10 (SCR-239) — the download state machine. The CLI argv contract and the
/// idle → downloading → installed | failed | cancelled transitions are pinned
/// here against a fake `model status --json` invoker (no running daemon).
@MainActor
final class ModelDownloadControllerTests: XCTestCase {

    final class FakeInvoker: @unchecked Sendable {
        private(set) var calls: [[String]] = []
        var respond: (([String]) throws -> Data?)?
        let lock = NSLock()

        func invoker() -> ModelDownloadController.JSONInvoker {
            { [weak self] args in
                self?.lock.lock(); self?.calls.append(args); self?.lock.unlock()
                if let data = try self?.respond?(args) { return data }
                return Data("{}".utf8)
            }
        }
    }

    private func statusJSON(
        state: String, done: Int = 0, total: Int = 0, reason: String? = nil,
        installed: Bool = false, size: Int = 2_000_000_000
    ) -> Data {
        let download: [String: Any] = [
            "ok": true, "schema_version": 1, "state": state, "model_id": "m",
            "bytes_done": done, "bytes_total": total, "reason": reason as Any? ?? NSNull(),
        ]
        let installedBlock: [String: Any] = [
            "ok": true, "schema_version": 1,
            "models": [["model_id": "qwen2.5-3b-instruct", "size_bytes": size, "installed": installed]],
        ]
        return try! JSONSerialization.data(
            withJSONObject: ["download": download, "installed": installedBlock]
        )
    }

    func testDownloadingStateDecodesProgress() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in self?.statusJSON(state: "downloading", done: 50, total: 100) }
        let c = ModelDownloadController(invoke: fake.invoker())

        await c.refreshStatus()

        XCTAssertEqual(c.state, .downloading(bytesDone: 50, bytesTotal: 100))
        XCTAssertEqual(c.state.fractionComplete, 0.5)
    }

    func testInstalledState() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in self?.statusJSON(state: "idle", installed: true) }
        let c = ModelDownloadController(invoke: fake.invoker())

        await c.refreshStatus()

        XCTAssertEqual(c.state, .installed)
        XCTAssertTrue(c.isDefaultModelInstalled)
        XCTAssertEqual(c.disclosedSizeBytes, 2_000_000_000)
    }

    func testFailedStateCarriesReason() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            self?.statusJSON(state: "failed", reason: "insufficient-disk:need-4.4GB-free")
        }
        let c = ModelDownloadController(invoke: fake.invoker())

        await c.refreshStatus()

        XCTAssertEqual(c.state, .failed(reason: "insufficient-disk:need-4.4GB-free"))
    }

    func testCancelledState() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in self?.statusJSON(state: "cancelled") }
        let c = ModelDownloadController(invoke: fake.invoker())
        await c.refreshStatus()
        XCTAssertEqual(c.state, .cancelled)
    }

    func testStartDownloadIssuesDownloadThenStatus() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            args == ["model", "download"] ? Data("started".utf8)
                : self?.statusJSON(state: "downloading", done: 1, total: 100)
        }
        let c = ModelDownloadController(invoke: fake.invoker())

        await c.startDownload()

        XCTAssertEqual(fake.calls.first, ["model", "download"])
        XCTAssertTrue(fake.calls.contains(["model", "status", "--json"]))
        XCTAssertTrue(c.state.isDownloading)
    }

    func testCancelIssuesCancelThenStatus() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            args == ["model", "cancel"] ? Data("ok".utf8) : self?.statusJSON(state: "cancelled")
        }
        let c = ModelDownloadController(invoke: fake.invoker())

        await c.cancel()

        XCTAssertEqual(fake.calls.first, ["model", "cancel"])
        XCTAssertEqual(c.state, .cancelled)
    }

    func testStatusFailureSurfacesErrorKeepsState() async {
        let fake = FakeInvoker()
        fake.respond = { _ in Data("{not-json".utf8) }
        let c = ModelDownloadController(invoke: fake.invoker())

        await c.refreshStatus()

        XCTAssertNotNil(c.lastError)
        XCTAssertEqual(c.state, .idle)  // unchanged from the initial state
    }
}
