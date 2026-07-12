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

    func testRelaunchHelperDefaultScriptNeverTouchesLaunchd() {
        // Release / non-dev relaunches must not mutate the login session's
        // launchd environment at all — the pre-fix script published the app's
        // PATH + repo root globally, leaking dev paths into every subsequently
        // launched app until logout (and re-publishing them on every relaunch).
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1
        )

        XCTAssertFalse(script.contains("launchctl"))
        XCTAssertTrue(script.contains("/usr/bin/open -n \"$2\""))
    }

    func testRelaunchHelperDevScriptScopesLaunchdPublicationToTheOpen() throws {
        // Dev-source runs publish PATH + repo root through launchd because it
        // is the only channel into an `open`-spawned process — but scoped:
        // set before the open, restored/removed right after, never left global.
        let script = PermissionController.relaunchHelperShellScript(
            maxPollCount: 3,
            pollIntervalSeconds: 0.1,
            publishDevEnvironment: true
        )

        let setPath = try XCTUnwrap(script.range(of: "/bin/launchctl setenv PATH \"$3\""))
        let setRoot = try XCTUnwrap(script.range(of: "/bin/launchctl setenv SCREENCAP_DEV_REPO_ROOT \"$4\""))
        let open = try XCTUnwrap(script.range(of: "/usr/bin/open -n \"$2\""))
        let restorePath = try XCTUnwrap(script.range(of: "setenv PATH \"$old_path\""))
        let restoreRoot = try XCTUnwrap(script.range(of: "setenv SCREENCAP_DEV_REPO_ROOT \"$old_root\""))

        XCTAssertLessThan(setPath.lowerBound, open.lowerBound)
        XCTAssertLessThan(setRoot.lowerBound, open.lowerBound)
        XCTAssertLessThan(open.upperBound, restorePath.lowerBound)
        XCTAssertLessThan(open.upperBound, restoreRoot.lowerBound)
        // Prior launchd values are restored, not clobbered — build_and_run.sh
        // publishes this same pair session-wide as the daemon LaunchAgent's
        // env channel, so the helper must leave the session exactly as found
        // (an unconditional unsetenv here broke the next daemon respawn).
        // Absent a prior value, the temporary one is removed.
        XCTAssertTrue(script.contains("/bin/launchctl unsetenv PATH"))
        XCTAssertTrue(script.contains("/bin/launchctl unsetenv SCREENCAP_DEV_REPO_ROOT"))
    }

    func testRelaunchHelperScriptsAreValidShellSyntax() throws {
        // The dev variant carries real control flow; `sh -n` parses without
        // executing, catching quoting/syntax regressions in both variants.
        for publish in [false, true] {
            let script = PermissionController.relaunchHelperShellScript(
                maxPollCount: 3,
                pollIntervalSeconds: 0.1,
                publishDevEnvironment: publish
            )
            let sh = Process()
            sh.executableURL = URL(fileURLWithPath: "/bin/sh")
            sh.arguments = ["-n", "-c", script]
            try sh.run()
            sh.waitUntilExit()
            XCTAssertEqual(
                sh.terminationStatus, 0,
                "publishDevEnvironment=\(publish) variant failed to parse"
            )
        }
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
        setupDismissed: Bool = false,
        migrationNeeded: Bool = false
    ) -> Bool {
        // These truth-table tests model the already-migrated (post-Phase-1c)
        // state by default; the migration-override cases live in
        // FirstRunSetupPresentationPolicyTests, and the SCR-262 converging
        // dimension defaults to the non-converging state there too — every
        // pin below maps unchanged onto the tri-state decision (wall ↔ true,
        // shell ↔ false).
        FirstRunSetupPresentationPolicy.launchPresentation(
            daemonProbeCompleted: daemonProbeCompleted,
            transport: transport,
            daemonGrants: daemonGrants,
            setupDismissed: setupDismissed,
            migrationNeeded: migrationNeeded
        ) == .permissionWall
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
        // Input Monitoring is DENIED here on purpose: it's not part of the
        // required set (it can't be granted to the helper on macOS 26.x), so
        // Screen Recording + Accessibility alone must clear the dismissal —
        // i.e. onboarding completes without IM. This is the exact scenario from
        // the SCR-196 follow-up bug.
        permissions.updateDaemonGrants(
            DaemonPermissionGrants(screenRecording: .granted, accessibility: .granted, inputMonitoring: .denied)
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

    // MARK: - SCR-143: "Finish setup" banner visibility (decoupled from launch gate)

    @MainActor
    func testFinishSetupBannerHiddenOnceRequiredPermissionsGranted() {
        // The regression: on the CLI-fallback path the daemon-grant auto-clear
        // never fires, so `setupDismissed` stays latched forever. The banner must
        // not key on `setupDismissed` alone — once the app process can record
        // (`allRequiredGranted`, the same predicate the CLI-fallback start gate
        // uses), the "can't record" banner must disappear even with no daemon.
        let (permissions, _) = makeController()
        permissions.markSetupDismissed()
        permissions._testSetRequiredPermissionsGranted(true)
        XCTAssertFalse(
            permissions.shouldShowFinishSetupBanner,
            "banner must clear once required permissions are granted, even with the daemon absent"
        )
    }

    @MainActor
    func testFinishSetupBannerShownWhenDismissedAndPermissionsMissing() {
        // Skipped the walkthrough AND still missing a required grant: the recovery
        // banner is the only way back into the walkthrough on CLI-fallback, so it
        // must show — and its "enable recording" claim is truthful here.
        let (permissions, _) = makeController()
        permissions.markSetupDismissed()
        permissions._testSetRequiredPermissionsGranted(false)
        XCTAssertTrue(permissions.shouldShowFinishSetupBanner)
    }

    @MainActor
    func testFinishSetupBannerHiddenWhenWalkthroughNeverSkipped() {
        // Never skipped → the launch gate owns setup; no recovery banner, even
        // while permissions are still missing.
        let (permissions, _) = makeController()
        permissions._testSetRequiredPermissionsGranted(false)
        XCTAssertFalse(permissions.shouldShowFinishSetupBanner)
    }

    @MainActor
    func testFinishSetupBannerShownWhenDismissedAndPermissionsNotDetermined() {
        // Pins the real pre-TCC-poll initial state: a fresh controller has all
        // required statuses at `.notDetermined` (no app-side TCC check has run).
        // Dismissed + not-yet-determined still can't record, so the banner shows.
        // A future widening of `allRequiredGranted` that treated `.notDetermined`
        // as granted would flip this to false and fail here.
        let (permissions, _) = makeController()
        permissions.markSetupDismissed()
        // Deliberately do NOT call _testSetRequiredPermissionsGranted: statuses
        // stay `.notDetermined`.
        XCTAssertTrue(permissions.shouldShowFinishSetupBanner)
    }

    @MainActor
    func testFinishSetupBannerShownPinsAllRequiredGrantedFalse() {
        // Pins the intermediate precondition the banner's truth depends on:
        // dismissed + a required grant denied means `allRequiredGranted` is false,
        // and only then is the banner's "can't record" claim truthful. Asserting
        // the precondition guards against a sign-flip to `setupDismissed &&
        // allRequiredGranted` passing incidentally.
        let (permissions, _) = makeController()
        permissions.markSetupDismissed()
        permissions._testSetRequiredPermissionsGranted(false)
        XCTAssertFalse(permissions.allRequiredGranted)
        XCTAssertTrue(permissions.shouldShowFinishSetupBanner)
    }

    @MainActor
    func testFinishSetupBannerClearsWhenPermissionsArriveAfterDismissal() {
        // Exercises the runtime sequence SCR-143 targets on one controller: the
        // user skips the walkthrough while a grant is missing (banner appears),
        // then later grants the permissions (banner clears) — the recovery banner
        // must track the live can-record state, not the latched dismissal alone.
        let (permissions, _) = makeController()
        permissions.markSetupDismissed()
        permissions._testSetRequiredPermissionsGranted(false)
        XCTAssertTrue(permissions.shouldShowFinishSetupBanner)

        permissions._testSetRequiredPermissionsGranted(true)
        XCTAssertFalse(permissions.shouldShowFinishSetupBanner)
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

    // MARK: - Phase 1c migration marker (SCR-49)

    /// A unique, not-yet-created temp base dir for an injected marker store.
    private func freshMarkerBase() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("screencap-permctl-migration", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
    }

    @MainActor
    func testMigrationNeededTrueWhenMarkerAbsent() {
        let base = freshMarkerBase()
        defer { try? FileManager.default.removeItem(at: base) }
        let permissions = PermissionController(
            migrationMarker: MigrationMarkerStore(baseDirectory: base)
        )
        XCTAssertTrue(permissions.migrationNeeded, "absent marker ⇒ migration still needed")
    }

    @MainActor
    func testMigrationNeededFalseWhenMarkerPresent() throws {
        let base = freshMarkerBase()
        defer { try? FileManager.default.removeItem(at: base) }
        try MigrationMarkerStore(baseDirectory: base).markMigrated()
        let permissions = PermissionController(
            migrationMarker: MigrationMarkerStore(baseDirectory: base)
        )
        XCTAssertFalse(permissions.migrationNeeded, "present marker ⇒ migration already done")
    }

    @MainActor
    func testMarkMigrationCompleteWritesMarkerAndClearsFlag() {
        let base = freshMarkerBase()
        defer { try? FileManager.default.removeItem(at: base) }
        let permissions = PermissionController(
            migrationMarker: MigrationMarkerStore(baseDirectory: base)
        )
        XCTAssertTrue(permissions.migrationNeeded)

        permissions.markMigrationComplete()

        XCTAssertFalse(permissions.migrationNeeded, "flag clears after completion")
        // Persisted: a fresh store over the same base reads the marker.
        XCTAssertTrue(MigrationMarkerStore(baseDirectory: base).isMigrated())
    }

    @MainActor
    func testMarkMigrationCompleteClearsFlagEvenWhenWriteFails() throws {
        // Base dir whose parent is a regular file → markMigrated() can't create
        // it and throws. The in-session flag must still clear (no same-session
        // re-nag); the marker stays absent so the banner returns next launch.
        let parent = FileManager.default.temporaryDirectory
            .appendingPathComponent("screencap-permctl-blocker-\(UUID().uuidString)")
        try Data().write(to: parent, options: .atomic)
        defer { try? FileManager.default.removeItem(at: parent) }
        let blockedBase = parent.appendingPathComponent("nested", isDirectory: true)

        let permissions = PermissionController(
            migrationMarker: MigrationMarkerStore(baseDirectory: blockedBase)
        )
        XCTAssertTrue(permissions.migrationNeeded)

        permissions.markMigrationComplete()

        XCTAssertFalse(permissions.migrationNeeded, "flag clears in-session even on write failure")
        XCTAssertFalse(
            MigrationMarkerStore(baseDirectory: blockedBase).isMigrated(),
            "marker did not persist, so the banner returns next launch"
        )
    }

    @MainActor
    func testMarkMigrationCompleteIsNoOpWhenAlreadyMigrated() throws {
        let base = freshMarkerBase()
        defer { try? FileManager.default.removeItem(at: base) }
        try MigrationMarkerStore(baseDirectory: base).markMigrated()
        let permissions = PermissionController(
            migrationMarker: MigrationMarkerStore(baseDirectory: base)
        )
        XCTAssertFalse(permissions.migrationNeeded)
        // Guard short-circuits — still false, no throw.
        permissions.markMigrationComplete()
        XCTAssertFalse(permissions.migrationNeeded)
    }


    // MARK: - SCR-201: row label is per-pane (SR rolls up to the app)

    func testHelperSettingsEntryNameIsPerPane() {
        // SCR-201 on-device fact: the panes do NOT share one row label.
        // - Screen Recording attributes to the responsible host app
        //   (com.screencap.macos) via the LoginItem rollup, so the row renders
        //   under the app's name, "ScreenCap".
        // - Accessibility and Input Monitoring attribute to the daemon's own
        //   identity (com.screencap.daemon), whose row renders as the helper
        //   bundle's filename, "ScreencapDaemon" (CFBundleDisplayName does not
        //   override it — SCR-200 U2).
        // - Microphone is app-owned → "ScreenCap".
        // The onboarding copy names the exact per-pane string so the user
        // toggles the right row (Accessibility has BOTH a "ScreenCap" decoy and
        // the real "ScreencapDaemon" row, so naming it precisely matters).
        XCTAssertEqual(PrivacyPane.screenRecording.helperSettingsEntryName, "ScreenCap")
        XCTAssertEqual(PrivacyPane.accessibility.helperSettingsEntryName, "ScreencapDaemon")
        XCTAssertEqual(PrivacyPane.inputMonitoring.helperSettingsEntryName, "ScreencapDaemon")
        XCTAssertEqual(PrivacyPane.microphone.helperSettingsEntryName, "ScreenCap")
    }

    // MARK: - SCR-200 U6 (R7): block-with-Retry heuristic (lives on the
    // permissions screen since the walkthrough sheet's retirement, U14)

    private func rowState(
        _ grant: DaemonGrantState,
        registering: Bool = false,
        elapsed: TimeInterval? = nil,
        budget: TimeInterval = 25
    ) -> OnboardingPermissionsStep.DaemonRowState {
        OnboardingPermissionsStep.daemonRowState(
            grant: grant,
            isRegistering: registering,
            elapsedSinceOpened: elapsed,
            budget: budget
        )
    }

    func testRowStateGrantedAlwaysWins() {
        // Granted short-circuits everything — even a long-elapsed budget.
        XCTAssertEqual(rowState(.granted, elapsed: 999), .granted)
    }

    func testRowStateInFlightShowsRegistering() {
        XCTAssertEqual(rowState(.denied, registering: true, elapsed: 999), .registering)
    }

    func testRowStateNeverBlocksBeforeUserReachesToggleStep() {
        // Not opened (elapsed nil): never block, regardless of denied state.
        XCTAssertEqual(rowState(.denied, elapsed: nil), .actionable)
    }

    func testRowStateWithinBudgetStaysActionable() {
        // Opened + denied but inside the budget → give the user time to toggle.
        XCTAssertEqual(rowState(.denied, elapsed: 5), .actionable)
    }

    func testRowStateBlocksWhenBudgetExhaustedAndStillDenied() {
        // R7: opened + still denied past the budget → block-with-Retry.
        XCTAssertEqual(rowState(.denied, elapsed: 25), .blockedWithRetry)
        XCTAssertEqual(rowState(.denied, elapsed: 60), .blockedWithRetry)
    }

    func testRowStateIndeterminateNeverHardBlocks() {
        // "Couldn't verify" keeps Retry available (actionable), never a hard
        // false-block — a transient probe hiccup must not block a granted user.
        XCTAssertEqual(rowState(.indeterminate, elapsed: 60), .actionable)
    }
}
