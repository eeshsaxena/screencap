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

    // MARK: - shouldPresentOnLaunch (Phase 1c migration dimension, SCR-49)

    /// Nothing presents until the daemon probe completes — even an unmigrated
    /// user (the migration override is still gated on a completed probe).
    func testNeverPresentsBeforeProbeCompletes() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: false,
                transport: .daemon,
                daemonGrants: denied,
                setupDismissed: false,
                migrationNeeded: true
            )
        )
    }

    /// Phase 1c: an unmigrated user sees the one-time banner even when the daemon
    /// already reports all grants — the migration override fires regardless of
    /// grant state.
    func testMigrationNeededPresentsEvenWhenDaemonSatisfied() {
        XCTAssertTrue(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .daemon,
                daemonGrants: notDenied,
                setupDismissed: false,
                migrationNeeded: true
            )
        )
    }

    /// Phase 1c: the migration banner overrides a prior "Skip for now" — a
    /// previously-dismissed user still gets the one-time upgrade explanation.
    func testMigrationNeededOverridesSetupDismissed() {
        XCTAssertTrue(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .daemon,
                daemonGrants: notDenied,
                setupDismissed: true,
                migrationNeeded: true
            )
        )
    }

    /// Once migrated, behavior is exactly the pre-Phase-1c gate: a satisfied,
    /// non-dismissed daemon does not present.
    func testMigratedSatisfiedDaemonDoesNotPresent() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .daemon,
                daemonGrants: notDenied,
                setupDismissed: false,
                migrationNeeded: false
            )
        )
    }

    /// Once migrated, a daemon reporting a required denial still presents
    /// (unchanged U4 behavior).
    func testMigratedDaemonDenialPresents() {
        XCTAssertTrue(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .daemon,
                daemonGrants: denied,
                setupDismissed: false,
                migrationNeeded: false
            )
        )
    }

    /// Once migrated, a prior "Skip for now" suppresses the launch gate
    /// (unchanged U4 behavior — the override only applies while migration is
    /// pending).
    func testMigratedSetupDismissedSuppresses() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .daemon,
                daemonGrants: denied,
                setupDismissed: true,
                migrationNeeded: false
            )
        )
    }

    /// Once migrated, the CLI-fallback path still always presents (unchanged).
    func testMigratedCliFallbackPresents() {
        XCTAssertTrue(
            FirstRunSetupPresentationPolicy.shouldPresentOnLaunch(
                daemonProbeCompleted: true,
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                setupDismissed: false,
                migrationNeeded: false
            )
        )
    }
}
