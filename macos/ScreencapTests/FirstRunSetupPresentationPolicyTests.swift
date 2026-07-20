import XCTest
@testable import Screencap

/// Presentation-decision tests for the permission-setup surface
/// (`FirstRunSetupPresentationPolicy`).
///
/// `MainWindow.updatePermissionSetupPresentation()` re-evaluates these on every
/// `daemonGrants` / `transport` / `daemonProbeCompleted` / `migrationNeeded` /
/// `updateConverging` change. Two regression families pinned here:
///
/// - SCR-144: a sheet reopened via the explicit "Finish setup" recovery latch
///   was auto-closed by a coincident daemon-grant / transport update before the
///   user could act (`shouldAutoCloseOnUpdate`'s `reopenedViaRecovery` flag).
/// - SCR-262: the first launch after an app update kickstarts a helper swap;
///   while it converges the daemon is unreachable and grants are unverifiable,
///   so the launch decision is the "Finishing update…" interstitial — never the
///   permission wall implying grants were lost. Non-converging unreachability
///   (a dead registration) still gets the wall immediately: it is the repair
///   surface, and only the explicit converging signal defers it.
final class FirstRunSetupPresentationPolicyTests: XCTestCase {

    private let notDenied = DaemonPermissionGrants(
        screenRecording: .granted, accessibility: .indeterminate, inputMonitoring: .granted
    )
    private let denied = DaemonPermissionGrants(
        screenRecording: .denied, accessibility: .granted, inputMonitoring: .granted
    )

    private func presentation(
        daemonProbeCompleted: Bool = true,
        transport: RecorderTransport,
        daemonGrants: DaemonPermissionGrants,
        setupDismissed: Bool = false,
        migrationNeeded: Bool = false,
        onboardingTakeoverActive: Bool = false,
        updateConverging: Bool = false,
        convergenceDeadlineExpired: Bool = false
    ) -> FirstRunSetupPresentationPolicy.LaunchPresentation {
        FirstRunSetupPresentationPolicy.launchPresentation(
            daemonProbeCompleted: daemonProbeCompleted,
            transport: transport,
            daemonGrants: daemonGrants,
            setupDismissed: setupDismissed,
            migrationNeeded: migrationNeeded,
            onboardingTakeoverActive: onboardingTakeoverActive,
            updateConverging: updateConverging,
            convergenceDeadlineExpired: convergenceDeadlineExpired
        )
    }

    // MARK: - shouldAutoCloseOnUpdate (SCR-144)

    /// SCR-144 regression: a sheet opened via the recovery latch must NOT be
    /// auto-closed even when the daemon path looks satisfied — otherwise the
    /// on-open daemon-grant refresh dismisses it before the user can act.
    func testRecoveryReopenSuppressesAutoCloseOnSatisfiedDaemon() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
                transport: .daemon,
                daemonGrants: notDenied,
                reopenedViaRecovery: true,
                migrationNeeded: false
            )
        )
    }

    /// Phase 1c: while the one-time migration banner is pending, a satisfied
    /// daemon must NOT auto-close the sheet — the banner is the leading step and
    /// a coincident grant refresh must not dismiss it out from under the user.
    func testMigrationPendingSuppressesAutoCloseOnSatisfiedDaemon() {
        XCTAssertFalse(
            FirstRunSetupPresentationPolicy.shouldAutoCloseOnUpdate(
                transport: .daemon,
                daemonGrants: notDenied,
                reopenedViaRecovery: false,
                migrationNeeded: true
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
                reopenedViaRecovery: false,
                migrationNeeded: false
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
                reopenedViaRecovery: false,
                migrationNeeded: false
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
                reopenedViaRecovery: false,
                migrationNeeded: false
            )
        )
    }

    // MARK: - launchPresentation (Phase 1c migration dimension, SCR-49)

    /// Nothing presents until the daemon probe completes — even an unmigrated
    /// user (the migration override is still gated on a completed probe).
    func testNeverPresentsBeforeProbeCompletes() {
        XCTAssertEqual(
            presentation(
                daemonProbeCompleted: false,
                transport: .daemon,
                daemonGrants: denied,
                migrationNeeded: true
            ),
            .shell
        )
    }

    /// Phase 1c: an unmigrated user sees the one-time banner even when the daemon
    /// already reports all grants — the migration override fires regardless of
    /// grant state.
    func testMigrationNeededPresentsEvenWhenDaemonSatisfied() {
        XCTAssertEqual(
            presentation(transport: .daemon, daemonGrants: notDenied, migrationNeeded: true),
            .permissionWall
        )
    }

    /// Phase 1c: the migration banner overrides a prior "Skip for now" — a
    /// previously-dismissed user still gets the one-time upgrade explanation.
    func testMigrationNeededOverridesSetupDismissed() {
        XCTAssertEqual(
            presentation(
                transport: .daemon,
                daemonGrants: notDenied,
                setupDismissed: true,
                migrationNeeded: true
            ),
            .permissionWall
        )
    }

    /// Once migrated, behavior is exactly the pre-Phase-1c gate: a satisfied,
    /// non-dismissed daemon does not present.
    func testMigratedSatisfiedDaemonDoesNotPresent() {
        XCTAssertEqual(
            presentation(transport: .daemon, daemonGrants: notDenied),
            .shell
        )
    }

    /// Once migrated, a daemon reporting a required denial still presents
    /// (unchanged U4 behavior).
    func testMigratedDaemonDenialPresents() {
        XCTAssertEqual(
            presentation(transport: .daemon, daemonGrants: denied),
            .permissionWall
        )
    }

    /// Once migrated, a prior "Skip for now" suppresses the launch gate
    /// (unchanged U4 behavior — the override only applies while migration is
    /// pending).
    func testMigratedSetupDismissedSuppresses() {
        XCTAssertEqual(
            presentation(transport: .daemon, daemonGrants: denied, setupDismissed: true),
            .shell
        )
    }

    /// SCR-262 scoping: NON-converging CLI fallback still presents the wall
    /// immediately — a dead registration (enabled label, gone bundle) never
    /// triggers a stale restart, and the wall's auto-fired helper install is
    /// its only repair path. Only the explicit converging signal defers the
    /// wall; bare unreachability never does.
    func testMigratedCliFallbackPresents() {
        XCTAssertEqual(
            presentation(transport: .cliFallback, daemonGrants: .allIndeterminate),
            .permissionWall
        )
    }

    // MARK: - launchPresentation (U11 onboarding-takeover dimension)

    /// U11: while the onboarding wizard owns the window, the sheet never
    /// presents — not even under the migration override, which is otherwise
    /// unconditional and would fire on every fresh install (whose migration
    /// marker is also absent) right over the wizard's welcome step.
    func testOnboardingTakeoverSuppressesSheetEvenWithMigrationPending() {
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: denied,
                migrationNeeded: true,
                onboardingTakeoverActive: true
            ),
            .shell
        )
    }

    // MARK: - launchPresentation (SCR-262 update-convergence dimension)

    /// AE1 pin: the update launch — helper swap converging, daemon unreachable,
    /// grants unverifiable — shows the interstitial, never the permission
    /// checklist.
    func testConvergingCliFallbackShowsInterstitial() {
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                updateConverging: true
            ),
            .updateInterstitial
        )
    }

    /// The deadline's fallback: a triggered-but-failed convergence reaches the
    /// wall (the repair surface). In production the loop clears converging at
    /// expiry, so both the both-flags tick and the settled state present the wall.
    func testConvergingDeadlineExpiredFallsThroughToWall() {
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                updateConverging: true,
                convergenceDeadlineExpired: true
            ),
            .permissionWall
        )
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                updateConverging: false,
                convergenceDeadlineExpired: true
            ),
            .permissionWall
        )
    }

    /// AE3 boundary: an answered DENIAL is genuine signal even mid-swap — the
    /// spurious-wall bug is about unverifiable rows, never an affirmative
    /// denied report. The wall presents without waiting out convergence.
    func testConvergingReachableDenialPresentsWall() {
        XCTAssertEqual(
            presentation(transport: .daemon, daemonGrants: denied, updateConverging: true),
            .permissionWall
        )
    }

    /// The doomed-daemon guard: mid-swap, a reachable daemon reporting grants
    /// intact is most likely the booted-out process still answering during its
    /// exit grace — hold the interstitial until the loop verifies freshness,
    /// rather than dropping to the shell on a transport about to die.
    func testConvergingReachableGrantedHoldsInterstitial() {
        XCTAssertEqual(
            presentation(transport: .daemon, daemonGrants: notDenied, updateConverging: true),
            .updateInterstitial
        )
    }

    /// The converging denial escape honors setupDismissed exactly like the
    /// settled gate: a user who persisted "Skip for now" must not get the wall
    /// mid-swap on a denial the non-converging path would suppress. The
    /// interstitial (honest status) still shows.
    func testConvergingDenialHonorsSetupDismissed() {
        XCTAssertEqual(
            presentation(
                transport: .daemon,
                daemonGrants: denied,
                setupDismissed: true,
                updateConverging: true
            ),
            .updateInterstitial
        )
    }

    /// The interstitial is status, not a permission nag — a persisted
    /// "Skip for now" does not suppress it.
    func testConvergingOverridesSetupDismissed() {
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                setupDismissed: true,
                updateConverging: true
            ),
            .updateInterstitial
        )
    }

    /// While converging, the migration banner defers — its wall would render the
    /// same unverifiable permission rows behind it. The banner resumes after
    /// convergence (migrationNeeded is untouched by the interstitial).
    func testConvergingPrecedesMigrationBanner() {
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                migrationNeeded: true,
                updateConverging: true
            ),
            .updateInterstitial
        )
    }

    /// The probe gate holds for the interstitial too — nothing presents before
    /// the sequenced first probe lands.
    func testConvergingNeverPresentsBeforeProbeCompletes() {
        XCTAssertEqual(
            presentation(
                daemonProbeCompleted: false,
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                updateConverging: true
            ),
            .shell
        )
    }

    /// U11 precedence: the onboarding wizard owns the window outright — a fresh
    /// install mid-swap (contrived, but reachable via replay) never sees the
    /// interstitial over the wizard.
    func testOnboardingTakeoverSuppressesInterstitial() {
        XCTAssertEqual(
            presentation(
                transport: .cliFallback,
                daemonGrants: .allIndeterminate,
                onboardingTakeoverActive: true,
                updateConverging: true
            ),
            .shell
        )
    }
}
