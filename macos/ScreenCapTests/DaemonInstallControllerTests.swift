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

    // SCR-121: a reachable daemon whose version != the bundle we would install
    // is a stale/foreign daemon squatting api.sock. It must not silently read as
    // "installed and running". We try the reinstall (refresh) path once; if it
    // still answers with the wrong version, surface a distinct failure.
    func testReachableDaemonWithWrongVersionSurfacesMismatchAfterReinstall() async {
        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.enabled],
            refreshStatuses: [.enabled]
        )
        let probe = FakeDaemonProbe(versions: ["0.12.7", "0.12.7"])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            expectedDaemonVersion: "0.20.0",
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.refreshedPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(probe.callCount, 2)
        XCTAssertEqual(controller.state, .installFailed(.daemonVersionMismatch))
    }

    func testReinstallRecoversWhenFreshDaemonReportsExpectedVersion() async {
        let registration = FakeDaemonRegistrationService(
            registerStatuses: [.enabled],
            refreshStatuses: [.enabled]
        )
        // Stale daemon answers first; after the refresh dislodges it the fresh
        // daemon reports the expected version.
        let probe = FakeDaemonProbe(versions: ["0.12.7", "0.20.0"])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            expectedDaemonVersion: "0.20.0",
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.refreshedPlistNames, [DaemonInstallController.plistName])
        XCTAssertEqual(controller.state, .installedAndRunning)
    }

    func testReachableDaemonWithMatchingVersionInstallsWithoutRefresh() async {
        let registration = FakeDaemonRegistrationService(registerStatuses: [.enabled])
        let probe = FakeDaemonProbe(versions: ["0.20.0"])
        let controller = DaemonInstallController(
            registrationService: registration,
            probe: probe,
            expectedDaemonVersion: "0.20.0",
            sleep: { _ in }
        )

        await controller.install(timeoutSeconds: 1, probeIntervalSeconds: 0.01)

        XCTAssertEqual(registration.refreshedPlistNames, [])
        XCTAssertEqual(probe.callCount, 1)
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
    /// Each element is the version a reachable daemon reports, or nil for an
    /// unreachable probe. The `results: [Bool]` initializer keeps the older
    /// reachability-only tests terse: `true` -> reachable with a placeholder
    /// version, `false` -> unreachable.
    private var results: [String?]
    private(set) var callCount = 0

    init(results: [Bool], version: String = "0.0.0-test") {
        self.results = results.map { $0 ? version : nil }
    }

    init(versions: [String?]) {
        self.results = versions
    }

    func probe(timeout: TimeInterval) async -> String? {
        callCount += 1
        return results.isEmpty ? nil : results.removeFirst()
    }
}
