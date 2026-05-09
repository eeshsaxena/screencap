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
}

@MainActor
private final class FakeDaemonRegistrationService: DaemonRegistrationService {
    private var registerStatuses: [SMAppService.Status]
    private var currentStatuses: [SMAppService.Status]
    private(set) var registeredPlistNames: [String] = []
    private(set) var currentStatusCallCount = 0

    init(
        registerStatuses: [SMAppService.Status],
        currentStatuses: [SMAppService.Status] = []
    ) {
        self.registerStatuses = registerStatuses
        self.currentStatuses = currentStatuses
    }

    func register(plistName: String) async throws -> SMAppService.Status {
        registeredPlistNames.append(plistName)
        return registerStatuses.isEmpty ? .enabled : registerStatuses.removeFirst()
    }

    func currentStatus(plistName: String) -> SMAppService.Status {
        currentStatusCallCount += 1
        return currentStatuses.isEmpty ? .requiresApproval : currentStatuses.removeFirst()
    }
}

@MainActor
private final class FakeDaemonProbe: DaemonProbe {
    private var results: [Bool]
    private(set) var callCount = 0

    init(results: [Bool]) {
        self.results = results
    }

    func probe(timeout: TimeInterval) async -> Bool {
        callCount += 1
        return results.isEmpty ? false : results.removeFirst()
    }
}
