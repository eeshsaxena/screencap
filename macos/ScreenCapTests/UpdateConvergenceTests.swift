import XCTest
@testable import ScreenCap

/// SCR-262 — the update-convergence machinery around the post-update helper
/// swap: the launch anchor decision, the re-probe loop's pure termination step
/// and its driven behavior, the interstitial/wall copy pins, and the
/// provenance-keyed dismissal persistence.
@MainActor
final class UpdateConvergenceTests: XCTestCase {

    private var defaults: UserDefaults!
    private var suiteName: String!
    private var recorder: RecorderController?

    override func setUp() {
        super.setUp()
        suiteName = "sc-update-convergence-\(UUID().uuidString.prefix(8))"
        defaults = UserDefaults(suiteName: suiteName)
        // Point the daemon socket at a nonexistent path so any probeDaemon()
        // the machinery runs fails fast to .unavailable instead of touching a
        // real daemon (mirrors RecorderControllerDaemonTests' isolation).
        setenv("SCREENCAP_DAEMON_SOCKET", "/tmp/sc-uc-\(UUID().uuidString.prefix(8)).sock", 1)
    }

    override func tearDown() async throws {
        recorder?._testCancelConvergenceLoop()
        await recorder?._testCancelDaemonTask()
        recorder = nil
        if let suiteName {
            defaults.removePersistentDomain(forName: suiteName)
        }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        try await super.tearDown()
    }

    // MARK: - convergenceAnchorForLaunch (the launch decision)

    func testTriggeredRestartAnchorsAtNow() {
        let now = Date(timeIntervalSince1970: 1_000)
        XCTAssertEqual(
            RecorderController.convergenceAnchorForLaunch(
                restartTriggered: true, persistedAnchor: nil, now: now, deadline: 90
            ),
            now
        )
    }

    /// A quit-and-relaunch inside the swap window re-enters convergence with the
    /// ORIGINAL anchor — no fresh deadline, no spurious wall.
    func testPersistedAnchorInsideDeadlineReentersWithOriginalAnchor() {
        let anchor = Date(timeIntervalSince1970: 1_000)
        let now = anchor.addingTimeInterval(30)
        XCTAssertEqual(
            RecorderController.convergenceAnchorForLaunch(
                restartTriggered: false, persistedAnchor: anchor, now: now, deadline: 90
            ),
            anchor
        )
    }

    func testPersistedAnchorPastDeadlineDoesNotConverge() {
        let anchor = Date(timeIntervalSince1970: 1_000)
        XCTAssertNil(
            RecorderController.convergenceAnchorForLaunch(
                restartTriggered: false,
                persistedAnchor: anchor,
                now: anchor.addingTimeInterval(90),
                deadline: 90
            )
        )
    }

    /// Clock-skew guard: a future-dated persisted anchor can't be trusted —
    /// mirror of the bundle-mtime guard in restartStaleDaemonIfNeeded.
    func testFutureDatedPersistedAnchorDoesNotConverge() {
        let now = Date(timeIntervalSince1970: 1_000)
        XCTAssertNil(
            RecorderController.convergenceAnchorForLaunch(
                restartTriggered: false,
                persistedAnchor: now.addingTimeInterval(60),
                now: now,
                deadline: 90
            )
        )
    }

    func testNoRestartNoAnchorDoesNotConverge() {
        XCTAssertNil(
            RecorderController.convergenceAnchorForLaunch(
                restartTriggered: false, persistedAnchor: nil, now: Date(), deadline: 90
            )
        )
    }

    // MARK: - convergenceStep (the loop's pure termination decision)

    private let anchor = Date(timeIntervalSince1970: 10_000)

    func testFreshDaemonFinishesConvergence() {
        XCTAssertEqual(
            RecorderController.convergenceStep(
                probeStartedAt: anchor.timeIntervalSince1970 + 5,
                anchor: anchor,
                now: anchor.addingTimeInterval(10),
                deadline: 90
            ),
            .finishedFresh
        )
    }

    /// The doomed-daemon guard: a probe answered by the pre-swap process
    /// (started BEFORE the trigger) is not convergence — launchd's exit grace
    /// lets it answer for up to ~30s after the kickstart.
    func testOldDaemonStillAnsweringKeepsWaiting() {
        XCTAssertEqual(
            RecorderController.convergenceStep(
                probeStartedAt: anchor.timeIntervalSince1970 - 3_600,
                anchor: anchor,
                now: anchor.addingTimeInterval(10),
                deadline: 90
            ),
            .keepWaiting
        )
    }

    func testUnreachableBeforeDeadlineKeepsWaiting() {
        XCTAssertEqual(
            RecorderController.convergenceStep(
                probeStartedAt: nil,
                anchor: anchor,
                now: anchor.addingTimeInterval(89),
                deadline: 90
            ),
            .keepWaiting
        )
    }

    func testUnreachablePastDeadlineExpires() {
        XCTAssertEqual(
            RecorderController.convergenceStep(
                probeStartedAt: nil,
                anchor: anchor,
                now: anchor.addingTimeInterval(90),
                deadline: 90
            ),
            .deadlineExpired
        )
    }

    /// Freshness wins at the deadline edge: a fresh daemon observed on the
    /// expiring tick still converges rather than falling to the wall.
    func testFreshDaemonAtDeadlineEdgeStillFinishes() {
        XCTAssertEqual(
            RecorderController.convergenceStep(
                probeStartedAt: anchor.timeIntervalSince1970 + 5,
                anchor: anchor,
                now: anchor.addingTimeInterval(300),
                deadline: 90
            ),
            .finishedFresh
        )
    }

    // MARK: - runLaunchDaemonCheck (sequencing + anchor persistence)

    func testTriggeredRestartEntersConvergenceAndPersistsAnchor() async {
        let recorder = RecorderController()
        self.recorder = recorder
        let now = Date()
        await recorder.runLaunchDaemonCheck(
            restartStaleDaemon: { true },
            defaults: defaults,
            now: { now }
        )
        XCTAssertTrue(recorder.updateConverging)
        XCTAssertEqual(recorder.updateConvergenceAnchor, now)
        XCTAssertEqual(
            defaults.object(forKey: RecorderController.updateConvergenceAnchorKey) as? Date, now
        )
        // Sequencing: the first probe ran (and completed) as part of the check.
        XCTAssertTrue(recorder.daemonProbeCompleted)
        XCTAssertEqual(recorder.transport, .cliFallback)
    }

    func testRelaunchInsideSwapWindowReentersConvergence() async {
        let original = Date().addingTimeInterval(-30)
        defaults.set(original, forKey: RecorderController.updateConvergenceAnchorKey)
        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.runLaunchDaemonCheck(
            restartStaleDaemon: { false },
            defaults: defaults
        )
        XCTAssertTrue(recorder.updateConverging)
        XCTAssertEqual(recorder.updateConvergenceAnchor, original)
    }

    func testRelaunchAfterDeadlineClearsStaleAnchorAndDoesNotConverge() async {
        defaults.set(
            Date().addingTimeInterval(-300),
            forKey: RecorderController.updateConvergenceAnchorKey
        )
        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.runLaunchDaemonCheck(
            restartStaleDaemon: { false },
            defaults: defaults
        )
        XCTAssertFalse(recorder.updateConverging)
        XCTAssertNil(defaults.object(forKey: RecorderController.updateConvergenceAnchorKey))
    }

    func testOrdinaryLaunchDoesNotConverge() async {
        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.runLaunchDaemonCheck(
            restartStaleDaemon: { false },
            defaults: defaults
        )
        XCTAssertFalse(recorder.updateConverging)
        XCTAssertFalse(recorder.updateConvergenceFailed)
        XCTAssertTrue(recorder.daemonProbeCompleted)
    }

    // MARK: - Convergence re-probe loop (driven with injected probe/clock/sleep)

    func testLoopClearsConvergenceWhenFreshDaemonAnswers() async {
        let recorder = RecorderController()
        self.recorder = recorder
        let anchor = Date()
        recorder._testBeginUpdateConvergence(anchor: anchor, defaults: defaults)

        let probes = LockedCounter()
        recorder.startConvergenceProbeLoopIfNeeded(
            freshProbe: {
                let n = probes.incrementAndGet()
                // Unreachable, unreachable, then the swapped-in daemon.
                return n < 3 ? nil : anchor.timeIntervalSince1970 + 5
            },
            now: { Date() },
            sleep: { _ in }
        )
        await waitUntil { !recorder.updateConverging }
        XCTAssertFalse(recorder.updateConvergenceFailed)
        XCTAssertEqual(probes.value, 3)
        XCTAssertNil(recorder.updateConvergenceAnchor)
        XCTAssertNil(defaults.object(forKey: RecorderController.updateConvergenceAnchorKey))
    }

    func testLoopFallsThroughToWallProvenanceAtDeadline() async {
        let recorder = RecorderController()
        self.recorder = recorder
        let anchor = Date()
        recorder._testBeginUpdateConvergence(anchor: anchor, defaults: defaults)

        let probes = LockedCounter()
        recorder.startConvergenceProbeLoopIfNeeded(
            freshProbe: { _ = probes.incrementAndGet(); return nil },
            // Two in-window ticks, then past the deadline.
            now: {
                probes.value < 2
                    ? anchor.addingTimeInterval(10)
                    : anchor.addingTimeInterval(RecorderController.updateConvergenceDeadline)
            },
            sleep: { _ in }
        )
        await waitUntil { !recorder.updateConverging }
        XCTAssertTrue(recorder.updateConvergenceFailed)
        XCTAssertNil(defaults.object(forKey: RecorderController.updateConvergenceAnchorKey))
    }

    func testLoopDoesNotStartWhenNotConverging() async {
        let recorder = RecorderController()
        self.recorder = recorder
        let probes = LockedCounter()
        recorder.startConvergenceProbeLoopIfNeeded(
            freshProbe: { _ = probes.incrementAndGet(); return nil },
            now: { Date() },
            sleep: { _ in }
        )
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(probes.value, 0)
        XCTAssertFalse(recorder.updateConverging)
    }

    // MARK: - Copy pins (honesty gate: status only, no permission claims)

    func testInterstitialCopyPins() {
        XCTAssertEqual(UpdateConvergenceCopy.headline, "Finishing update…")
        XCTAssertEqual(
            UpdateConvergenceCopy.body,
            "ScreenCap is restarting its recording helper to pick up the update. "
            + "This usually takes under a minute — no action needed."
        )
        XCTAssertEqual(
            UpdateConvergenceCopy.prolongedWait,
            "Still working — this can take a little longer on some Macs."
        )
        XCTAssertEqual(
            UpdateConvergenceCopy.deadlineFallbackNotice,
            "The update didn't finish cleanly, so ScreenCap needs to check its recording helper."
        )
    }

    /// The interstitial must never imply grants were lost — that is the exact
    /// harm it exists to remove.
    func testInterstitialCopyMakesNoPermissionClaims() {
        for text in [
            UpdateConvergenceCopy.headline,
            UpdateConvergenceCopy.body,
            UpdateConvergenceCopy.prolongedWait,
        ] {
            for banned in ["permission", "grant", "denied", "System Settings"] {
                XCTAssertFalse(
                    text.localizedCaseInsensitiveContains(banned),
                    "interstitial copy must not claim permission state: \(text)"
                )
            }
        }
    }

    func testProlongedWaitLineJoinsAtThreshold() {
        XCTAssertNil(UpdateConvergenceCopy.statusLine(elapsedSeconds: 0))
        XCTAssertNil(
            UpdateConvergenceCopy.statusLine(
                elapsedSeconds: UpdateConvergenceCopy.prolongedWaitThreshold - 1
            )
        )
        XCTAssertEqual(
            UpdateConvergenceCopy.statusLine(
                elapsedSeconds: UpdateConvergenceCopy.prolongedWaitThreshold
            ),
            UpdateConvergenceCopy.prolongedWait
        )
    }

    // MARK: - PermissionSetupDismissalPolicy (KTD-8 / AE4)

    /// AE4: Continue on the wall reached via deadline expiry must not persist —
    /// a later launch with a genuine denial still presents the wall.
    func testDismissalNotPersistedOnUpdateConvergenceWall() {
        XCTAssertFalse(
            PermissionSetupDismissalPolicy.shouldPersistDismissal(
                allRequiredDaemonGrantsGranted: false,
                reachedViaUpdateConvergence: true
            )
        )
    }

    /// The ordinary non-converging wall (dead registration, `.cliFallback`)
    /// persists exactly as today — the repair wall stays skippable.
    func testDismissalPersistedOnOrdinaryWall() {
        XCTAssertTrue(
            PermissionSetupDismissalPolicy.shouldPersistDismissal(
                allRequiredDaemonGrantsGranted: false,
                reachedViaUpdateConvergence: false
            )
        )
    }

    /// All grants satisfied → nothing to persist (the auto-close path).
    func testDismissalNotPersistedWhenAllGranted() {
        XCTAssertFalse(
            PermissionSetupDismissalPolicy.shouldPersistDismissal(
                allRequiredDaemonGrantsGranted: true,
                reachedViaUpdateConvergence: false
            )
        )
    }

    // MARK: - helpers

    private func waitUntil(
        timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ predicate: @escaping @MainActor () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("Timed out waiting for condition", file: file, line: line)
    }
}

private final class LockedCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = 0

    var value: Int {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func incrementAndGet() -> Int {
        lock.lock()
        defer { lock.unlock() }
        storage += 1
        return storage
    }
}
