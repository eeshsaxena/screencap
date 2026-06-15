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
    func testRequestReopenSetupLatchesAndConsumesWithoutClearingDismissal() {
        let (permissions, defaults) = makeController()
        permissions.markSetupDismissed()
        XCTAssertFalse(permissions.reopenSetupRequested)

        // The recovery entry point latches a request for MainWindow to observe.
        permissions.requestReopenSetup()
        XCTAssertTrue(permissions.reopenSetupRequested)
        // It must NOT clear the persisted dismissal — only completing setup
        // (daemon grants landing) re-arms the launch gate, so a user who taps
        // "Finish setup" then closes the sheet again still isn't re-nagged on
        // the next launch.
        XCTAssertTrue(permissions.setupDismissed)
        XCTAssertTrue(defaults.bool(forKey: "com.screencap.macos.permissionSetupDismissed"))

        // MainWindow consumes the latch after presenting so it doesn't
        // re-present on a later view update.
        permissions.consumeReopenSetupRequest()
        XCTAssertFalse(permissions.reopenSetupRequested)
        XCTAssertTrue(permissions.setupDismissed)
    }

    @MainActor
    func testAdHocDevBuildWarningGatesOnAdHocSigningAndAnActiveDenial() {
        let suite = "test-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)

        func controller(adHoc: Bool) -> PermissionController {
            PermissionController(defaults: defaults, isAdHocBuild: adHoc)
        }
        let denied = DaemonPermissionGrants(
            screenRecording: .denied, accessibility: .granted, inputMonitoring: .granted
        )
        let allGranted = DaemonPermissionGrants(
            screenRecording: .granted, accessibility: .granted, inputMonitoring: .granted
        )

        // Signed build never shows the dev hint, even with a denial — the OS
        // really did orphan/deny it, but it's not the ad-hoc treadmill.
        let signed = controller(adHoc: false)
        signed.updateDaemonGrants(denied)
        XCTAssertFalse(signed.showAdHocDevBuildWarning)

        // Ad-hoc build with everything granted: nothing confusing to explain.
        let adhocOK = controller(adHoc: true)
        adhocOK.updateDaemonGrants(allGranted)
        XCTAssertFalse(adhocOK.showAdHocDevBuildWarning)

        // Ad-hoc build reporting a denial: the exact "granted in Settings but
        // denied here" case → surface the hint.
        let adhocDenied = controller(adHoc: true)
        adhocDenied.updateDaemonGrants(denied)
        XCTAssertTrue(adhocDenied.showAdHocDevBuildWarning)
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

    @MainActor
    func testDaemonGrantWatchingReopenAfterStopMidRefreshStillKicks() async {
        let (permissions, _) = makeController()

        // First session: a slow refresh we deliberately leave in-flight.
        let slow = RefreshCounter()
        permissions.startDaemonGrantWatching {
            try? await Task.sleep(nanoseconds: 300_000_000)
            await slow.bump()
        }
        // Close the sheet while that refresh is still awaiting.
        permissions.stopDaemonGrantWatching()

        // Reopen immediately: the immediate on-open refresh MUST still fire even
        // though the prior refresh hasn't completed (the in-flight guard must
        // not be stuck true across the stop).
        let reopened = RefreshCounter()
        permissions.startDaemonGrantWatching { await reopened.bump() }
        try? await Task.sleep(nanoseconds: 100_000_000)
        let reopenedCount = await reopened.count
        XCTAssertGreaterThanOrEqual(
            reopenedCount, 1,
            "re-open must kick a fresh refresh even after a stop during an in-flight refresh"
        )

        permissions.stopDaemonGrantWatching()
    }

    // MARK: - U8: daemon-driven registration Grant flow

    @MainActor
    private func makeRegisteringController(
        registrar: @escaping PermissionController.DaemonPermissionRegistrar,
        opener: @escaping @MainActor (PrivacyPane) -> Void
    ) -> PermissionController {
        let suite = "test-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return PermissionController(
            defaults: defaults,
            daemonRegistrar: registrar,
            daemonSettingsOpener: opener
        )
    }

    func testPrivacyPanePermissionStringMatchesDaemonContract() {
        XCTAssertEqual(PrivacyPane.screenRecording.permissionString, "screen_recording")
        XCTAssertEqual(PrivacyPane.accessibility.permissionString, "accessibility")
        XCTAssertEqual(PrivacyPane.inputMonitoring.permissionString, "input_monitoring")
        // Microphone is not a daemon-registered permission.
        XCTAssertNil(PrivacyPane.microphone.permissionString)
        // Round-trips back through from(permissionString:).
        for pane in [PrivacyPane.screenRecording, .accessibility, .inputMonitoring] {
            XCTAssertEqual(PrivacyPane.from(permissionString: pane.permissionString!), pane)
        }
    }

    @MainActor
    func testDaemonRegistrationCallsRegistrarThenOpensSettings() async {
        // AE2: the daemon registers for the permission, then the app opens the
        // matching pane — ack BEFORE pane-open, with the correct permission.
        var events: [String] = []
        let permissions = makeRegisteringController(
            registrar: { permission in events.append("register:\(permission)") },
            opener: { pane in events.append("open:\(pane.rawValue)") }
        )

        await permissions.requestDaemonPermission(for: .accessibility)

        XCTAssertEqual(events, ["register:accessibility", "open:accessibility"])
    }

    @MainActor
    func testDaemonRegistrationRoutesEachPaneToItsPermission() async {
        // R6: not screen-recording-only — each required pane forwards its own
        // canonical permission string to the daemon verb.
        for (pane, expected) in [
            (PrivacyPane.screenRecording, "screen_recording"),
            (.accessibility, "accessibility"),
            (.inputMonitoring, "input_monitoring"),
        ] {
            var registered: [String] = []
            let permissions = makeRegisteringController(
                registrar: { permission in registered.append(permission) },
                opener: { _ in }
            )
            await permissions.requestDaemonPermission(for: pane)
            XCTAssertEqual(registered, [expected])
        }
    }

    @MainActor
    func testDaemonRegistrationOpensSettingsEvenWhenRegistrarThrows() async {
        // Best-effort: a failed registration must still open the pane so the
        // user can enable the helper manually (never a dead end), and the
        // in-flight guard must be cleared on the error path.
        var openedPanes: [PrivacyPane] = []
        let permissions = makeRegisteringController(
            registrar: { _ in throw DaemonClientError.timedOut(seconds: 1) },
            opener: { pane in openedPanes.append(pane) }
        )

        await permissions.requestDaemonPermission(for: .inputMonitoring)

        XCTAssertEqual(openedPanes, [.inputMonitoring])
        XCTAssertFalse(permissions.isDaemonRegistering(.inputMonitoring))
    }

    @MainActor
    func testDaemonRegistrationPerPaneInFlightGuardCollapsesConcurrentTaps() async {
        // Concurrency (U5 guard, realized in U8): a second tap while a round-trip
        // is outstanding is a no-op — exactly one registration in flight per pane.
        var registrarCallCount = 0
        let permissions = makeRegisteringController(
            registrar: { _ in
                registrarCallCount += 1
                try? await Task.sleep(nanoseconds: 200_000_000)
            },
            opener: { _ in }
        )

        async let first: Void = permissions.requestDaemonPermission(for: .screenRecording)
        // Let the first call enter the in-flight state before the second tap.
        try? await Task.sleep(nanoseconds: 60_000_000)
        XCTAssertTrue(permissions.isDaemonRegistering(.screenRecording))
        await permissions.requestDaemonPermission(for: .screenRecording)  // no-op
        await first

        XCTAssertEqual(registrarCallCount, 1)
        XCTAssertFalse(permissions.isDaemonRegistering(.screenRecording))
    }

    @MainActor
    func testDaemonRegistrationSkipsSettingsOpenWhenWalkthroughDismissedMidRequest() async {
        // Review #2: the registrar round-trip can take up to the client timeout.
        // If the user dismisses the walkthrough while it is in flight, opening
        // System Settings afterward pops a pane out of nowhere. A walkthrough-
        // originated request (watch active at start) must suppress the open when
        // the walkthrough has since been dismissed (watch stopped in onDisappear).
        var openedPanes: [PrivacyPane] = []
        let permissions = makeRegisteringController(
            registrar: { _ in try? await Task.sleep(nanoseconds: 200_000_000) },
            opener: { pane in openedPanes.append(pane) }
        )
        permissions.startDaemonGrantWatching {}
        XCTAssertTrue(permissions.isDaemonGrantWatching)

        async let request: Void = permissions.requestDaemonPermission(for: .accessibility)
        // Let the request reach its in-flight await, then dismiss the walkthrough.
        try? await Task.sleep(nanoseconds: 60_000_000)
        permissions.stopDaemonGrantWatching()
        await request

        XCTAssertEqual(
            openedPanes, [],
            "Settings must not open after the walkthrough was dismissed mid-request"
        )
    }

    @MainActor
    func testDaemonRegistrationOpensSettingsWhenWalkthroughStaysVisible() async {
        // The gate must not over-block: while the walkthrough is still visible,
        // a completed registration opens the matching pane as before.
        var openedPanes: [PrivacyPane] = []
        let permissions = makeRegisteringController(
            registrar: { _ in },
            opener: { pane in openedPanes.append(pane) }
        )
        permissions.startDaemonGrantWatching {}
        defer { permissions.stopDaemonGrantWatching() }

        await permissions.requestDaemonPermission(for: .accessibility)

        XCTAssertEqual(openedPanes, [.accessibility])
    }

    @MainActor
    func testRequestAndOpenSettingsDaemonSubjectInvokesRegistrar() async {
        // The synchronous public entrypoint (used by the Grant button) routes
        // the .daemon subject into the async registration round-trip — no longer
        // the old no-op that just opened the pane.
        var registered: [String] = []
        let permissions = makeRegisteringController(
            registrar: { permission in registered.append(permission) },
            opener: { _ in }
        )

        permissions.requestAndOpenSettings(for: .screenRecording, subject: .daemon)

        // The Task is fire-and-forget; poll briefly until it runs.
        for _ in 0..<100 where registered.isEmpty {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(registered, ["screen_recording"])
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
