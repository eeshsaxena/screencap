import ServiceManagement
import XCTest
@testable import ScreenCap

@MainActor
final class DaemonInstallControllerTests: XCTestCase {
    func testHappyPathRegistersAndPollsDaemonInfo() async {
        let registration = FakeDaemonRegistrationService(registerStatuses: [.enabled])
        let probe = FakeDaemonProbe(results: [true])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.registeredPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(probe.callCount, 1)
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    func testRequiresApprovalPollsUntilEnabledThenInstalls() async {
        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.requiresApproval],
            currentStatuses: [.requiresApproval, .enabled]
        )
        let probe = FakeDaemonProbe(results: [true])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            sleep: { _ in }
        )

        await controller.install(
            timeoutSeconds: 1,
            probeIntervalSeconds: 0.01,
            approvalTimeoutSeconds: 1,
            approvalPollIntervalSeconds: 0.01
        )

        XCTAssertEqual(registration.currentStatusCallCount, 2)
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    func testRequiresApprovalTimeoutFailsUnknown() async {
        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.requiresApproval],
            currentStatuses: [.requiresApproval, .requiresApproval, .requiresApproval]
        )
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: FakeDaemonProbe(results: []),
            sleep: { _ in }
        )

        await controller.install(
            timeoutSeconds: 1,
            probeIntervalSeconds: 0.01,
            approvalTimeoutSeconds: 0,
            approvalPollIntervalSeconds: 0.01
        )

        XCTAssertEqual(controller.state, .installFailed(.unknown))
    }

    func testProbeTimeoutSurfacesPollingFailed() async {
        let controller = DaemonInstallController(
            registrationService: FakeDaemonRegistrationService(registerStatuses: [.enabled]),
            probe: FakeDaemonProbe(results: [false, false, false]),
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 0, probeIntervalSeconds: 0.01)

        XCTAssertEqual(controller.state, .pollingFailed(reason: "Daemon did not respond within 0s"))
    }

    func testEnabledDaemonThatDoesNotRespondRefreshesRegistrationOnce() async {
        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.enabled],
            refreshStatuses: [.enabled]
        )
        let probe = FakeDaemonProbe(results: [false, true])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 0, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.registeredPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(registration.refreshedPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(probe.callCount, 2)
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    func testProbeFailureKeepsPollingUntilTimeout() async {
        let probe = FakeDaemonProbe(results: [false, false, true])
        let controller = DaemonInstallController(
            registrationService: FakeDaemonRegistrationService(registerStatuses: [.enabled]),
            probe: probe,
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(probe.callCount, 3)
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    func testRetryRerunsInstallFromFailure() async {
        let registration = FakeDaemonRegistrationService(registerStatuses: [.notFound, .enabled])
        let probe = FakeDaemonProbe(results: [true])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)
        XCTAssertEqual(controller.state, .installFailed(.plistWriteFailed))

        await controller.retry(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.registeredPlistNames.count, 2)
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    func testInstallSuccessPostsDaemonInstalledNotification() async {
        let expectation = expectation(description: "daemon installed notification")
        let observer = NotificationCenter.default.addObserver(
            forName: .screenCapDaemonInstalledAndRunning,
            object: nil,
            queue: .main
        ) { _ in expectation.fulfill() }
        defer { NotificationCenter.default.removeObserver(observer) }

        let controller = DaemonInstallController(
            registrationService: FakeDaemonRegistrationService(registerStatuses: [.enabled]),
            probe: FakeDaemonProbe(results: [true]),
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        await fulfillment(of: [expectation], timeout: 1)
    }

    // SCR-121 / SCR-135: a daemon that PERSISTS at the wrong version through the
    // reinstall is a real, non-recoverable mismatch — surface it. The post-refresh
    // poll gives the swap a convergence budget rather than failing on the first
    // probe, so this only fails after the budget expires with the stale daemon
    // still answering. The stale daemon is booted out before the reinstall.
    func testReachableDaemonWithWrongVersionSurfacesMismatchAfterReinstall() async {
        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.enabled],
            refreshStatuses: [.enabled]
        )
        let terminator = FakeDaemonTerminator()
        // Sustained wrong version: the stale daemon never goes away.
        let probe = FakeDaemonProbe(versions: ["0.12.7"])
        let clock = FakeClock()
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            terminator: terminator,
            expectedDaemonVersion: "0.20.0",
            sleep: { clock.advance(nanoseconds: $0) },
            now: { clock.now }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.5)

        XCTAssertEqual(registration.refreshedPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(terminator.bootoutCount, 1)
        XCTAssertEqual(controller.state, .installFailed(.daemonVersionMismatch))
    }

    // SCR-135 regression: the swap from the stale daemon to the freshly installed
    // one is NOT instantaneous — the old daemon keeps answering api.sock until it
    // exits and the new one can bind. The post-refresh poll must keep probing
    // (convergence budget) until the expected version appears, rather than
    // concluding a false mismatch on the first second-pass probe. Before the
    // budget fix this sequence (stale answering twice across the reinstall)
    // produced a false `.daemonVersionMismatch`.
    func testReinstallRecoversWhenFreshDaemonReportsExpectedVersion() async {
        let expectation = expectation(description: "daemon installed notification")
        let observer = NotificationCenter.default.addObserver(
            forName: .screenCapDaemonInstalledAndRunning,
            object: nil,
            queue: .main
        ) { _ in expectation.fulfill() }
        defer { NotificationCenter.default.removeObserver(observer) }

        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.enabled],
            refreshStatuses: [.enabled]
        )
        let terminator = FakeDaemonTerminator()
        // Stale daemon answers on the first poll AND on the first post-refresh
        // probe (it has not exited yet); only the second post-refresh probe sees
        // the fresh daemon's expected version.
        let probe = FakeDaemonProbe(versions: ["0.12.7", "0.12.7", "0.20.0"])
        let clock = FakeClock()
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            terminator: terminator,
            expectedDaemonVersion: "0.20.0",
            sleep: { clock.advance(nanoseconds: $0) },
            now: { clock.now }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.5)

        XCTAssertEqual(registration.refreshedPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(terminator.bootoutCount, 1)
        XCTAssertEqual(controller.state, .installedAndRunning)
        await fulfillment(of: [expectation], timeout: 1)
    }

    func testReachableDaemonWithMatchingVersionInstallsWithoutRefresh() async {
        let registration = FakeDaemonRegistrationService(registerStatuses: [.enabled])
        let terminator = FakeDaemonTerminator()
        let probe = FakeDaemonProbe(versions: ["0.20.0"])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            terminator: terminator,
            expectedDaemonVersion: "0.20.0",
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.refreshedPlistNames, [])
        XCTAssertEqual(probe.callCount, 1)
        XCTAssertEqual(terminator.bootoutCount, 0)
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    // Fail-safe: when the expected version can't be determined (no bundled CLI,
    // dev-source mode, unreadable stamp) we preserve the historical
    // reachable-implies-running behavior rather than risk a false "out of date".
    func testUnknownExpectedVersionAdoptsReachableDaemon() async {
        let probe = FakeDaemonProbe(versions: ["0.12.7"])
        let controller = DaemonInstallController(
            registrationService: FakeDaemonRegistrationService(registerStatuses: [.enabled]),
            probe: probe,
            expectedDaemonVersion: nil,
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(controller.state, .installedAndRunning)
    }
}

@MainActor
private final class FakeDaemonRegistrationService: DaemonRegistrationService {
    private var registerStatuses: [SMAppService.Status]
    private var refreshStatuses: [SMAppService.Status]
    private var currentStatuses: [SMAppService.Status]
    private(set) var registeredPlistNames: [String] = []
    private(set) var refreshedPlistNames: [String] = []
    private(set) var currentStatusCallCount = 0

    init(
        registerStatuses: [SMAppService.Status],
        refreshStatuses: [SMAppService.Status] = [],
        currentStatuses: [SMAppService.Status] = []
    ) {
        self.registerStatuses = registerStatuses
        self.refreshStatuses = refreshStatuses
        self.currentStatuses = currentStatuses
    }

    func register(plistName: String) async throws -> SMAppService.Status {
        registeredPlistNames.append(plistName)
        return registerStatuses.isEmpty ? .enabled : registerStatuses.removeFirst()
    }

    func refresh(plistName: String) async throws -> SMAppService.Status {
        refreshedPlistNames.append(plistName)
        return refreshStatuses.isEmpty ? .enabled : refreshStatuses.removeFirst()
    }

    func currentStatus(plistName: String) -> SMAppService.Status {
        currentStatusCallCount += 1
        return currentStatuses.isEmpty ? .requiresApproval : currentStatuses.removeFirst()
    }
}

@MainActor
private final class FakeDaemonProbe: DaemonProbe {
    /// A scripted per-poll sequence: each element is the version a reachable
    /// daemon reports, or nil for an unreachable probe. The `results: [Bool]`
    /// initializer keeps the older reachability-only tests terse: `true` ->
    /// reachable with a placeholder version, `false` -> unreachable.
    ///
    /// Once the script is exhausted the LAST value is sustained on every further
    /// probe, so a convergence-budget poll (SCR-135) that out-probes the script
    /// keeps seeing the same daemon state instead of an artificial "vanished"
    /// nil. A sustained mismatch models a daemon that never goes away; a
    /// sequence like `["0.12.7", "0.12.7", "0.20.0"]` models a non-instantaneous
    /// swap that eventually converges.
    private var results: [String?]
    private var sustained: String?
    private(set) var callCount = 0

    init(results: [Bool], version: String = "0.0.0-test") {
        self.results = results.map { $0 ? version : nil }
    }

    init(versions: [String?]) {
        self.results = versions
    }

    func probe(timeout: TimeInterval) async -> String? {
        callCount += 1
        guard !results.isEmpty else { return sustained }
        sustained = results.removeFirst()
        return sustained
    }
}

/// Deterministic clock driven by the controller's stubbed `sleep`, so the
/// convergence-budget poll (SCR-135) reaches its deadline in a fixed number of
/// probes instead of spinning against a real wall clock. Accessed only on the
/// main actor (the controller is @MainActor and so are the tests).
private final class FakeClock {
    private var current = Date(timeIntervalSinceReferenceDate: 0)
    var now: Date { current }
    func advance(nanoseconds: UInt64) {
        current = current.addingTimeInterval(Double(nanoseconds) / 1_000_000_000)
    }
}

@MainActor
private final class FakeDaemonTerminator: DaemonTerminator {
    private(set) var bootoutCount = 0
    func bootout() async { bootoutCount += 1 }
}
