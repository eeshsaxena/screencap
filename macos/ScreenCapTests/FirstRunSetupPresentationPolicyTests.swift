import XCTest
@testable import ScreenCap

/// Auto-close decision tests for the first-run permission walkthrough
/// (`FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate`).
///
/// `MainWindow.updateFirstRunSheetPresentation()` re-evaluates this on every
/// `daemonGrants` / `transport` / `daemonProbeCompleted` change while the sheet
/// is up. The regression these pin is SCR-144: a sheet reopened via the explicit
/// "Finish setup" recovery latch was auto-closed by a coincident daemon-grant /
/// transport update before the user could act. The close decision now keys on a
/// `reopenedViaRecovery` flag that suppresses it for the recovery sheet's
/// lifetime, without weakening the documented launch-path auto-close.
final class FirstRunSetupPresentationPolicyTests: XCTestCase {

    private let notDenied = DaemonPermissionGrants(
        screenRecording: .granted, accessibility: .indeterminate, inputMonitoring: .granted
    )
    private let denied = DaemonPermissionGrants(
        screenRecording: .denied, accessibility: .granted, inputMonitoring: .granted
    )

    /// SCR-144 regression: a sheet opened via the recovery latch must NOT be
    /// auto-closed even when the daemon path looks satisfied — otherwise the
    /// on-open daemon-grant refresh dismisses it before the user can act.
    func testRecoveryReopenSuppressesAutoCloseOnSatisfiedDaemon() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
                transport: .daemon,
                daemonGrants: notDenied,
                reopenedViaRecovery: true
            )
        )
    }

    /// Launch path preserved: with the recovery flag clear, a reachable daemon
    /// reporting no required denial still auto-closes (cold-boot bounce, U5
    /// post-grant refresh).
    func testLaunchPathAutoClosesOnSatisfiedDaemon() {
        XCTAssertTrue(
            FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
                transport: .daemon,
                daemonGrants: notDenied,
                reopenedViaRecovery: false
            )
        )
    }

    /// The verified-safe CLI-fallback path never auto-closes — the close branch
    /// is gated on `transport == .daemon`.
    func testCliFallbackNeverAutoCloses() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
                transport: .cliFallback,
                daemonGrants: notDenied,
                reopenedViaRecovery: false
            )
        )
    }

    /// A reachable daemon still reporting a required denial keeps the walkthrough
    /// up regardless of how it was opened.
    func testDaemonRequiredDenialKeepsWalkthroughUp() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
                transport: .daemon,
                daemonGrants: denied,
                reopenedViaRecovery: false
            )
        )
    }
}
