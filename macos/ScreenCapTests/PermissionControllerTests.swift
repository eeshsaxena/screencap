import XCTest
@testable import ScreenCap

private actor RefreshCounter {
    private(set) var count = 0
    func bump() { count += 1 }
}

final class PermissionControllerTests: XCTestCase {
    @MainActor
    func testPermissionControllerStartsWithoutAppTCCChecks() {
        let permissions = PermissionController()

        XCTAssertEqual(permissions.screenRecording, .notDetermined)
        XCTAssertEqual(permissions.accessibility, .notDetermined)
        XCTAssertEqual(permissions.inputMonitoring, .notDetermined)
        XCTAssertEqual(permissions.microphone, .notDetermined)
    }

    @MainActor
    func testPermissionSheetDismissesBeforeRelaunching() async {
        var events: [String] = []

        await PermissionSheetRelaunchFlow.dismissThenRelaunch(
            dismiss: { events.append("dismiss") },
            relaunch: { events.append("relaunch") },
            sleep: { nanoseconds in
                XCTAssertEqual(nanoseconds, PermissionSheetRelaunchFlow.sheetDismissalDelayNanoseconds)
                events.append("delay")
            }
        )

        XCTAssertEqual(events, ["dismiss", "delay", "relaunch"])
    }

    func testRelaunchHelperExitsOnTimeoutInsteadOfOpeningNewInstance() {
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1
        )

        XCTAssertTrue(script.contains("[ $i -ge 3 ] && exit 0"))
        XCTAssertFalse(script.contains("[ $i -ge 3 ] && break"))
    }

    func testRelaunchHelperReopensAppBundleThroughLaunchServices() {
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1
        )

        XCTAssertTrue(script.contains("/usr/bin/open -n \"$2\""))
    }

    func testRelaunchHelperPublishesDevEnvironmentBeforeOpeningBundle() {
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1
        )

        XCTAssertTrue(script.contains("/bin/launchctl setenv PATH \"$3\""))
        XCTAssertTrue(script.contains("/bin/launchctl setenv SCREENCAP_DEV_REPO_ROOT \"$4\""))
    }

    func testDaemonTCCSubjectUsesSamePrivacyPaneDeepLinks() {
        // settingsURL returns the same destination regardless of subject,
        // because TCC panes list every subject in one list. Subject-specific
        // behavior lives in `requestAndOpenSettings` (request flow gate).
        for pane in [PrivacyPane.screenRecording, .accessibility, .inputMonitoring] {
            XCTAssertEqual(
                PermissionController.settingsURL(for: pane),
                pane.deepLinkURL
            )
        }

        XCTAssertEqual(PermissionSubject.daemon.bundleIdentifier, "com.screencap.daemon")
    }

    // MARK: - U4: walkthrough gate truth table (keyed on daemon grant state)

    private static func grants(
        screen: DaemonGrantState,
        accessibility: DaemonGrantState = .granted,
        input: DaemonGrantState = .granted
    ) -> DaemonPermissionGrants {
        DaemonPermissionGrants(
            screenRecording: screen,
            accessibility: accessibility,
            inputMonitoring: input
        )
    }

    private func present(
        daemonProbeCompleted: Bool = true,
        transport: RecorderTransport,
        daemonGrants: DaemonPermissionGrants,
        setupDismissed: Bool = false
    ) -> Bool {
        FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
            daemonProbeCompleted: daemonProbeCompleted,
            transport: transport,
            daemonGrants: daemonGrants,
            setupDismissed: setupDismissed
        )
    }

    func testFirstRunSetupWaitsForDaemonProbeBeforePresenting() {
        XCTAssertFalse(
            present(
                daemonProbeCompleted: false,
                transport: .cliFallback,
                daemonGrants: .allIndeterminate
            )
        )
    }

    func testFirstRunSetupPresentsWhenDaemonReachableButRequiredGrantDenied() {
        // AE1: daemon reachable, Screen Recording denied → present.
        XCTAssertTrue(present(transport: .daemon, daemonGrants: Self.grants(screen: .denied)))
    }

    func testFirstRunSetupPresentsWhenDaemonReachableAndAdvisoryGrantDenied() {
        // Any required denial surfaces the walkthrough (even an advisory one the
        // start-block wouldn't hard-block on) — R3 is broader than R4.
        XCTAssertTrue(
            present(transport: .daemon, daemonGrants: Self.grants(screen: .granted, accessibility: .denied))
        )
    }

    func testFirstRunSetupDoesNotPresentWhenDaemonReportsAllGranted() {
        XCTAssertFalse(present(transport: .daemon, daemonGrants: Self.grants(screen: .granted)))
    }

    func testFirstRunSetupDoesNotPresentWhenDaemonGrantIndeterminate() {
        // Indeterminate is never "missing" — the engine preflight is the backstop.
        XCTAssertFalse(
            present(transport: .daemon, daemonGrants: Self.grants(screen: .indeterminate))
        )
        XCTAssertFalse(present(transport: .daemon, daemonGrants: .allIndeterminate))
    }

    func testFirstRunSetupPresentsOnCLIFallbackRegardlessOfDaemonGrants() {
        // Daemon unreachable — preserve the existing CLI-path walkthrough.
        XCTAssertTrue(present(transport: .cliFallback, daemonGrants: .allIndeterminate))
        XCTAssertTrue(present(transport: .cliFallback, daemonGrants: Self.grants(screen: .granted)))
    }

    func testFirstRunSetupSuppressedWhenDismissed() {
        // A persisted dismissal stops the gate re-popping — both on the daemon
        // path with a real denial and on the CLI-fallback path.
        XCTAssertFalse(
            present(transport: .daemon, daemonGrants: Self.grants(screen: .denied), setupDismissed: true)
        )
        XCTAssertFalse(
            present(transport: .cliFallback, daemonGrants: .allIndeterminate, setupDismissed: true)
        )
    }

    // MARK: - U4: dismissal flag persistence + auto-clear

    @MainActor
    private func makeController() -> (PermissionController, UserDefaults) {
        let suite = "test-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return (PermissionController(defaults: defaults), defaults)
    }

    @MainActor
    func testMarkSetupDismissedPersists() {
        let (permissions, defaults) = makeController()
        XCTAssertFalse(permissions.setupDismissed)
        permissions.markSetupDismissed()
        XCTAssertTrue(permissions.setupDismissed)
        XCTAssertTrue(defaults.bool(forKey: "com.screencap.macos.permissionSetupDismissed"))
        // Survives across controller instances backed by the same defaults.
        let reloaded = PermissionController(defaults: defaults)
        XCTAssertTrue(reloaded.setupDismissed)
    }

    @MainActor
    func testAllRequiredDaemonGrantsClearsDismissal() {
        let (permissions, _) = makeController()
        permissions.markSetupDismissed()
        XCTAssertTrue(permissions.setupDismissed)

        // A still-missing grant must NOT clear the dismissal.
        permissions.updateDaemonGrants(
            DaemonPermissionGrants(screenRecording: .denied, accessibility: .granted, inputMonitoring: .granted)
        )
        XCTAssertTrue(permissions.setupDismissed)

        // All required granted re-arms the walkthrough for a future loss.
        permissions.updateDaemonGrants(
            DaemonPermissionGrants(screenRecording: .granted, accessibility: .granted, inputMonitoring: .granted)
        )
        XCTAssertFalse(permissions.setupDismissed)
    }

    @MainActor
    func testDaemonGrantPerPaneMappingExcludesMicrophone() {
        let (permissions, _) = makeController()
        permissions.updateDaemonGrants(
            DaemonPermissionGrants(screenRecording: .granted, accessibility: .denied, inputMonitoring: .indeterminate)
        )
        XCTAssertEqual(permissions.daemonGrant(for: .screenRecording), .granted)
        XCTAssertEqual(permissions.daemonGrant(for: .accessibility), .denied)
        XCTAssertEqual(permissions.daemonGrant(for: .inputMonitoring), .indeterminate)
        // Microphone is not a daemon-tracked permission.
        XCTAssertEqual(permissions.daemonGrant(for: .microphone), .indeterminate)
        XCTAssertFalse(permissions.allRequiredDaemonGrantsGranted)
    }

    // MARK: - U5: daemon-grant refresh lifecycle + row icons

    @MainActor
    func testDaemonGrantWatchingStartsStopsAndKicksImmediateRefresh() async {
        let (permissions, _) = makeController()
        let counter = RefreshCounter()
        XCTAssertFalse(permissions.isDaemonGrantWatching)

        permissions.startDaemonGrantWatching { await counter.bump() }
        XCTAssertTrue(permissions.isDaemonGrantWatching)

        // An immediate refresh runs on open so the rows reflect current state
        // without waiting a full 5s timer tick.
        try? await Task.sleep(nanoseconds: 100_000_000)
        let kicks = await counter.count
        XCTAssertGreaterThanOrEqual(kicks, 1)

        permissions.stopDaemonGrantWatching()
        XCTAssertFalse(permissions.isDaemonGrantWatching)
    }

    func testGrantRowIconsAreThreeDistinctStates() {
        // Indeterminate must read as "couldn't verify" — never a granted check
        // or a denied needs-action. All three labels must be distinct.
        XCTAssertEqual(FirstRunPermissionsView.grantRowIcon(for: .granted).accessibilityLabel, "Granted")
        XCTAssertEqual(FirstRunPermissionsView.grantRowIcon(for: .denied).accessibilityLabel, "Needs action")
        XCTAssertEqual(
            FirstRunPermissionsView.grantRowIcon(for: .indeterminate).accessibilityLabel,
            "Couldn't verify"
        )
        let labels = Set(
            [DaemonGrantState.granted, .denied, .indeterminate]
                .map { FirstRunPermissionsView.grantRowIcon(for: $0).accessibilityLabel }
        )
        XCTAssertEqual(labels.count, 3)
    }
}
