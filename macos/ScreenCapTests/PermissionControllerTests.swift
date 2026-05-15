import XCTest
@testable import ScreenCap

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

    func testFirstRunSetupWaitsForDaemonProbeBeforePresenting() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: false,
                transport: .cliFallback
            )
        )
    }

    func testFirstRunSetupDoesNotPresentForDaemonTransport() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .daemon
            )
        )
    }

    func testFirstRunSetupPresentsWhenDaemonProbeFallsBackToCLI() {
        XCTAssertTrue(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .cliFallback
            )
        )
    }
}
