import XCTest
@testable import Screencap

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

    /// The daemon-side cancel converges lazily (the transfer itself is not
    /// interruptible), so the post-cancel status read can still say
    /// `downloading` — it must NOT resurrect the poll loop the user just
    /// stopped (KTD8's auto-start is for downloads observed elsewhere, not
    /// for undoing an explicit Cancel).
    func testCancelDoesNotResurrectPollingOnStaleDownloadingRead() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            args == ["model", "cancel"] ? Data("ok".utf8)
                : self?.statusJSON(state: "downloading", done: 50, total: 100)
        }
        let c = ModelDownloadController(invoke: fake.invoker())
        await c.refreshStatus()
        XCTAssertTrue(c.isPolling, "seeded mid-download: the loop runs")

        await c.cancel()

        XCTAssertFalse(c.isPolling,
                       "a stale post-cancel `downloading` read must not restart the loop")
        XCTAssertTrue(c.state.isDownloading,
                      "the stale state itself renders honestly until the daemon converges")
    }

    // MARK: - SCR-293 — stale download progress

    /// Flips the fake between healthy and throwing so a test can drive the
    /// controller through a real read-failure sequence.
    private final class Gate: @unchecked Sendable {
        private let lock = NSLock()
        private var value = false
        var failing: Bool {
            get { lock.lock(); defer { lock.unlock() }; return value }
            set { lock.lock(); value = newValue; lock.unlock() }
        }
    }

    /// Seeds a controller mid-download whose reads then start throwing.
    private func downloadingThenFailing() -> (ModelDownloadController, Gate) {
        let gate = Gate()
        let payload = statusJSON(state: "downloading", done: 50, total: 100)
        let c = ModelDownloadController(invoke: { _ in
            if gate.failing {
                throw NSError(domain: "t", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "socket down"])
            }
            return payload
        })
        return (c, gate)
    }

    private func offer(_ c: ModelDownloadController) -> ModelDownloadOffer {
        ModelDownloadOffer.render(
            state: c.state,
            progressStale: c.isDownloadProgressStale,
            daemonUnreachable: c.daemonUnreachable
        )
    }

    /// SCR-293 — the bug: `refreshStatus` deliberately leaves `state` at
    /// `.downloading` when a read fails, so a surface switching on `state`
    /// alone shows a bar frozen at its last reading, forever, with no error and
    /// no way out. The rendered offer must reach `.stalled` — but only after
    /// the debounce, so one blip on the ~1s poll cadence doesn't flash an error
    /// over a healthy download.
    func testFailingReadsDuringDownloadRenderStalledAfterDebounce() async {
        let (c, gate) = downloadingThenFailing()
        await c.refreshStatus()
        XCTAssertEqual(offer(c), .downloading(fractionComplete: 0.5))

        gate.failing = true

        await c.refreshStatus()
        XCTAssertEqual(offer(c), .downloading(fractionComplete: 0.5),
                       "a single failed read is ordinary jitter, not a stall")

        await c.refreshStatus()
        XCTAssertEqual(offer(c), .stalled,
                       "consecutive failed reads mean the bar is stale — say so, don't freeze")
        XCTAssertTrue(c.state.isDownloading,
                      "the controller's no-blank invariant still holds; only the render changes")
    }

    /// The stall is advisory, not terminal: the daemon coming back heals the
    /// surface on the next good read.
    func testSuccessfulReadClearsStalledRender() async {
        let (c, gate) = downloadingThenFailing()
        await c.refreshStatus()
        gate.failing = true
        await c.refreshStatus()
        await c.refreshStatus()
        XCTAssertEqual(offer(c), .stalled)

        gate.failing = false
        await c.refreshStatus()

        XCTAssertEqual(offer(c), .downloading(fractionComplete: 0.5))
        XCTAssertEqual(c.consecutiveCLIFailures, 0)
    }

    /// The other door into the same frozen bar: Cancel with the CLI down stops
    /// the poll loop (KTD8's guard) while `state` stays `.downloading`, so
    /// nothing is left to heal the surface. Both round-trips it makes must
    /// count, or the user lands on a frozen bar with the loop already dead.
    func testFailedCancelRendersStalled() async {
        let (c, gate) = downloadingThenFailing()
        await c.refreshStatus()
        gate.failing = true

        await c.cancel()

        XCTAssertFalse(c.isPolling, "cancel stops the loop that would have healed this")
        XCTAssertEqual(offer(c), .stalled,
                       "with no poll loop left, the surface must offer recovery itself")
    }

    /// Staleness is a claim about a *rendered download*, so failed reads with
    /// nothing in flight never render `.stalled`. They aren't nothing, though:
    /// an enabled Download button that can't start is the same swallowed error
    /// the frozen bar was, so the offer goes `.unavailable` instead.
    func testFailingReadsWithoutDownloadRenderUnavailableNotStalled() async {
        let c = ModelDownloadController(invoke: { _ in
            throw NSError(domain: "t", code: 1, userInfo: [:])
        })

        await c.refreshStatus()
        await c.refreshStatus()

        XCTAssertFalse(c.isDownloadProgressStale)
        XCTAssertEqual(offer(c), .unavailable)
        XCTAssertTrue(c.daemonUnreachable, "the KTD3 flag keeps its first-failure semantics")
    }

    /// The debounce buys a second opinion from the poll loop's next tick. Cancel
    /// stops that loop (KTD8 forbids resurrecting it), so when the cancel verb
    /// SUCCEEDS and only the follow-up read fails, the counter reaches 1 with
    /// nothing left to retry — waiting for a second failure would leave exactly
    /// the frozen bar SCR-293 removes.
    func testCancelSucceedingThenFailingReadRendersStalled() async {
        let gate = Gate()
        let payload = statusJSON(state: "downloading", done: 50, total: 100)
        let c = ModelDownloadController(invoke: { args in
            // Only the status read fails; `model cancel` itself succeeds.
            if args.contains("--json"), gate.failing {
                throw NSError(domain: "t", code: 1, userInfo: [:])
            }
            return payload
        })
        await c.refreshStatus()
        XCTAssertTrue(c.isPolling, "seeded mid-download: the loop runs")

        gate.failing = true
        await c.cancel()

        XCTAssertEqual(c.consecutiveCLIFailures, 1, "the cancel verb succeeded; only the read failed")
        XCTAssertFalse(c.isPolling)
        XCTAssertEqual(offer(c), .stalled,
                       "with the loop gone, one failed read is already terminal")
    }

    /// A sustained outage must not keep spawning a CLI subprocess every second:
    /// once reads stop landing there is nothing left to animate, so the cadence
    /// backs off to a ceiling. The debounce window itself stays fast — the
    /// second opinion `isDownloadProgressStale` waits for has to arrive
    /// promptly, or the frozen bar simply lasts longer.
    func testPollCadenceBacksOffOnlyAfterTheDebounceWindow() async {
        let gate = Gate()
        let payload = statusJSON(state: "downloading", done: 50, total: 100)
        let c = ModelDownloadController(invoke: { _ in
            if gate.failing { throw NSError(domain: "t", code: 1, userInfo: [:]) }
            return payload
        })
        await c.refreshStatus()
        XCTAssertEqual(c.nextPollDelayNanos, 1_000_000_000, "healthy: the smooth cadence")

        gate.failing = true
        await c.refreshStatus()
        XCTAssertEqual(c.nextPollDelayNanos, 1_000_000_000,
                       "inside the debounce window the next read must stay prompt")

        var seen: [UInt64] = []
        for _ in 0..<6 {
            await c.refreshStatus()
            seen.append(c.nextPollDelayNanos)
        }
        XCTAssertEqual(seen, [2, 4, 8, 16, 30, 30].map { $0 * 1_000_000_000 },
                       "doubles past the threshold, then holds at the 30s ceiling")

        gate.failing = false
        await c.refreshStatus()
        XCTAssertEqual(c.nextPollDelayNanos, 1_000_000_000,
                       "one good read restores the smooth cadence")
    }

    /// The terminal arms ignore both advisory flags — a stale/unreachable read
    /// must never downgrade a finished or failed download.
    func testTerminalStatesIgnoreStaleAndUnreachableFlags() {
        XCTAssertEqual(
            ModelDownloadOffer.render(
                state: .installed, progressStale: true, daemonUnreachable: true),
            .installed)
        XCTAssertEqual(
            ModelDownloadOffer.render(
                state: .failed(reason: "insufficient-disk"),
                progressStale: true, daemonUnreachable: true),
            .failed(reason: "insufficient-disk"))
    }
}
