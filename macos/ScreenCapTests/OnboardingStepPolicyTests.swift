import XCTest
@testable import ScreenCap

/// U11 — pure derivation tests for the onboarding wizard (KTD-10): the
/// takeover decision (wizard vs marker-backfill vs none), the derived step on
/// (re)launch, grant-driven auto-advance, tier-driven dot counts and routing,
/// the KTD-9 honesty gate over the storage/account copy, and the marker
/// store's prior-install evidence probe.
final class OnboardingStepPolicyTests: XCTestCase {

    // MARK: - Takeover decision

    /// Fresh install: no marker, no pending flag, no evidence → wizard.
    func testFreshInstallPresentsWizard() {
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: false, wizardPending: false,
                priorInstallEvidence: false, setupSkipped: false, isRecording: false
            ),
            .wizard
        )
    }

    /// Existing user (recordings/config/LaunchAgent present) with no marker:
    /// the marker is backfilled write-once and the wizard never shows — the
    /// migration interstitial (sheet flow) remains their upgrade surface.
    func testPriorInstallEvidenceBackfillsMarkerInsteadOfWizard() {
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: false, wizardPending: false,
                priorInstallEvidence: true, setupSkipped: false, isRecording: false
            ),
            .backfillMarker
        )
    }

    /// The completion marker wins over everything — no wizard, no backfill.
    func testCompletedMarkerSuppressesTakeover() {
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: true, wizardPending: true,
                priorInstallEvidence: true, setupSkipped: false, isRecording: false
            ),
            .none
        )
    }

    /// Mid-wizard relaunch: the pending flag keeps the wizard presenting even
    /// though the app's own first-launch `config.toml` write now reads as
    /// prior-install evidence. This is the launch-2 fresh-install case.
    func testPendingFlagOutranksPriorInstallEvidence() {
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: false, wizardPending: true,
                priorInstallEvidence: true, setupSkipped: false, isRecording: false
            ),
            .wizard
        )
    }

    /// "Skip for now" persists the dismissal: the takeover stops re-popping
    /// (recovery latch remains the way back), and the stabilizing backfill
    /// still happens once evidence exists.
    func testSetupSkippedSuppressesWizard() {
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: false, wizardPending: true,
                priorInstallEvidence: false, setupSkipped: true, isRecording: false
            ),
            .none
        )
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: false, wizardPending: false,
                priorInstallEvidence: true, setupSkipped: true, isRecording: false
            ),
            .backfillMarker
        )
    }

    /// Never take over the window while a recording is in flight.
    func testRecordingSuppressesTakeover() {
        XCTAssertEqual(
            OnboardingStepPolicy.takeover(
                completed: false, wizardPending: true,
                priorInstallEvidence: false, setupSkipped: false, isRecording: true
            ),
            .none
        )
    }

    // MARK: - Step derivation (derived, never stored — KTD-10)

    /// Fresh install lands on the welcome step.
    func testInitialStepFreshInstallIsWelcome() {
        XCTAssertEqual(
            OnboardingStepPolicy.initialStep(wizardStarted: false, requiredGrantsGranted: false),
            .welcome
        )
    }

    /// Relaunch mid-wizard with grants still missing re-derives to the
    /// permissions step, not welcome.
    func testInitialStepMidWizardUngrantedIsPermissions() {
        XCTAssertEqual(
            OnboardingStepPolicy.initialStep(wizardStarted: true, requiredGrantsGranted: false),
            .permissions
        )
    }

    /// Relaunch mid-wizard after the grants landed (the Quit & Relaunch loop)
    /// re-derives past permissions to app rules — the "granted → step 2, not
    /// step 0" scenario.
    func testInitialStepMidWizardGrantedIsAppRules() {
        XCTAssertEqual(
            OnboardingStepPolicy.initialStep(wizardStarted: true, requiredGrantsGranted: true),
            .appRules
        )
    }

    // MARK: - Auto-advance on grant detection

    /// Grant detected while the permissions step is up → advance (the
    /// `shouldAutoCloseOnUpdate` analog).
    func testAutoAdvancesFromPermissionsWhenGranted() {
        XCTAssertTrue(
            OnboardingStepPolicy.shouldAutoAdvance(from: .permissions, requiredGrantsGranted: true)
        )
    }

    /// Indeterminate/denied never auto-advances, and no other step reacts to
    /// grant changes (a grant landing while the user reads the storage step
    /// must not yank them backwards or forwards).
    func testAutoAdvanceOnlyFiresOnPermissionsStep() {
        XCTAssertFalse(
            OnboardingStepPolicy.shouldAutoAdvance(from: .permissions, requiredGrantsGranted: false)
        )
        for step in OnboardingStep.allCases where step != .permissions {
            XCTAssertFalse(
                OnboardingStepPolicy.shouldAutoAdvance(from: step, requiredGrantsGranted: true),
                "unexpected auto-advance from \(step)"
            )
        }
    }

    // MARK: - Helper install auto-start

    /// Permissions surface visible, installer untouched, daemon not connected
    /// → drive `install()` without a click. This is the self-heal trigger for
    /// a registration that reads `.enabled` while its recorded bundle path is
    /// gone (deleted dev worktree, app moved after first launch): launchd
    /// keeps `spawn failed` forever, and only `install()`'s poll-timeout →
    /// registration-refresh path can repair it — a state the idle card's
    /// "approve it first" copy would otherwise dead-end, because Login Items
    /// already shows the helper approved.
    func testAutoStartsHelperInstallWhenIdleOffDaemonTransport() {
        XCTAssertTrue(
            OnboardingStepPolicy.shouldAutoStartHelperInstall(
                installerState: .idle, transport: .cliFallback,
                daemonProbeCompleted: true, replay: false
            )
        )
    }

    /// Only the untouched `.idle` state auto-starts: in-flight states must not
    /// double-fire, and failure states keep their explicit user-driven retry
    /// (no auto-retry loop against a persistently broken install).
    func testAutoStartOnlyFiresFromIdle() {
        let nonIdle: [DaemonInstallController.State] = [
            .registering, .polling, .pollingFailed(reason: "x"),
            .installedAndRunning, .requiresApproval, .installFailed(.unknown),
        ]
        for state in nonIdle {
            XCTAssertFalse(
                OnboardingStepPolicy.shouldAutoStartHelperInstall(
                    installerState: state, transport: .cliFallback,
                    daemonProbeCompleted: true, replay: false
                ),
                "unexpected auto-start from \(state)"
            )
        }
    }

    /// A connected daemon needs no install — never touch a healthy machine.
    func testAutoStartSkipsOnDaemonTransport() {
        XCTAssertFalse(
            OnboardingStepPolicy.shouldAutoStartHelperInstall(
                installerState: .idle, transport: .daemon,
                daemonProbeCompleted: true, replay: false
            )
        )
    }

    /// Before the launch probe settles, transport's `.cliFallback` is a
    /// default, not a verdict — firing then could destructively re-register a
    /// healthy daemon that simply hadn't been probed yet. The step re-fires
    /// the check when `daemonProbeCompleted` flips.
    func testAutoStartWaitsForLaunchProbe() {
        XCTAssertFalse(
            OnboardingStepPolicy.shouldAutoStartHelperInstall(
                installerState: .idle, transport: .cliFallback,
                daemonProbeCompleted: false, replay: false
            )
        )
    }

    /// Replay is read-only by contract — it must not mutate helper state.
    func testAutoStartSkipsInReplay() {
        XCTAssertFalse(
            OnboardingStepPolicy.shouldAutoStartHelperInstall(
                installerState: .idle, transport: .cliFallback,
                daemonProbeCompleted: true, replay: true
            )
        )
    }

    // MARK: - Tier routing + dots

    /// Local continues to the SCR-239 download-model step; Personal and Team
    /// continue to the account step; Team continues again to team setup.
    func testTierRouting() {
        XCTAssertEqual(OnboardingStepPolicy.stepAfterStorage(tier: .local), .downloadModel)
        XCTAssertEqual(OnboardingStepPolicy.stepAfterStorage(tier: .personalCloud), .account)
        XCTAssertEqual(OnboardingStepPolicy.stepAfterStorage(tier: .teamCloud), .account)
        XCTAssertNil(OnboardingStepPolicy.stepAfterAccount(tier: .personalCloud))
        XCTAssertEqual(OnboardingStepPolicy.stepAfterAccount(tier: .teamCloud), .teamSetup)
    }

    /// Progress-dot count is 5/5/6 by picked tier (local gained the SCR-239
    /// download-model step).
    func testDotCountByTier() {
        XCTAssertEqual(OnboardingStepPolicy.dotCount(tier: .local), 5)
        XCTAssertEqual(OnboardingStepPolicy.dotCount(tier: .personalCloud), 5)
        XCTAssertEqual(OnboardingStepPolicy.dotCount(tier: .teamCloud), 6)
    }

    /// SCR-239 (U11) — the download-model step is the local tier's 5th dot
    /// (index 4), not present for cloud tiers, and never colliding with
    /// account(4)/teamSetup(5).
    func testDownloadModelStepDotAndRouting() {
        XCTAssertEqual(OnboardingStepPolicy.activeDotIndex(for: .downloadModel), 4)
        XCTAssertEqual(OnboardingStepPolicy.activeDotIndex(for: .storage), 3)
        // The download-model step is the local tier's 5th of 5 dots.
        XCTAssertEqual(
            OnboardingStepPolicy.activeDotIndex(for: .downloadModel) + 1,
            OnboardingStepPolicy.dotCount(tier: .local)
        )
    }

    // MARK: - Honesty gate (KTD-9 / R7)

    /// The storage and account steps carry no end-to-end-encryption or
    /// present-tense team-sharing claims. The Personal card now DOES carry its
    /// real price ($5/mo, billing U7 / R2) — honest and billable on a
    /// server-readable tier — so pricing is no longer forbidden here; the E2EE
    /// "we can't watch" claims still are (R2), and the Team card stays
    /// price-free while coming-soon (checked below).
    func testStorageAndAccountCopyCarriesNoForbiddenClaims() {
        let all = OnboardingCopy.storageAndAccountStrings.joined(separator: " ").lowercased()
        for forbidden in ["encrypt", "e2e", "keys stay", "we can't watch"] {
            XCTAssertFalse(all.contains(forbidden), "storage/account copy contains \"\(forbidden)\"")
        }
        XCTAssertFalse(all.contains("shared team library"))
    }

    /// The Personal card shows its price now; the Team card (coming soon) must
    /// not carry any price until the team tier actually ships (R11).
    func testPersonalCardPricedButTeamCardIsNot() {
        XCTAssertTrue(
            OnboardingCopy.personalCardMeta.contains("$5"),
            "Personal card should surface its $5/mo price"
        )
        let teamStrings = ([OnboardingCopy.teamCardTitle, OnboardingCopy.teamCardMeta]
            + OnboardingCopy.teamCardBullets).joined(separator: " ").lowercased()
        for forbidden in ["$", "month", "/mo"] {
            XCTAssertFalse(
                teamStrings.contains(forbidden),
                "Team card must stay price-free while coming soon; found \"\(forbidden)\""
            )
        }
    }

    /// The welcome cards (the design's "Encrypted sharing … Always." card),
    /// the step-2 footer (the design's "Private browser windows always pause
    /// recording"), and the team-setup step (the design's "one shared,
    /// encrypted library") also carry no untrue claims.
    func testWelcomeAppRulesAndTeamCopyCarriesNoForbiddenClaims() {
        var strings = OnboardingCopy.welcomeCards.flatMap { [$0.title, $0.body] }
        strings.append(OnboardingCopy.appRulesFooter)
        strings.append(OnboardingCopy.teamHeadline)
        strings.append(OnboardingCopy.teamSub)
        let all = strings.joined(separator: " ").lowercased()
        for forbidden in ["encrypt", "e2e", "always pause", "pause recording"] {
            XCTAssertFalse(all.contains(forbidden), "copy contains \"\(forbidden)\"")
        }
    }

    // MARK: - Marker store (prior-install evidence + write-once completion)

    private func makeTempDir() throws -> URL {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("onboarding-store-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.removeItem(at: dir)
        }
        return dir
    }

    func testEvidenceProbeDetectsEachSignal() throws {
        let base = try makeTempDir()
        let agents = try makeTempDir()
        let store = OnboardingMarkerStore(
            baseDirectory: base, launchAgentsDirectory: agents,
            defaults: UserDefaults(suiteName: "onboarding-tests-\(UUID().uuidString)")!
        )
        XCTAssertFalse(store.hasPriorInstallEvidence())

        // Each signal independently counts as evidence.
        let recordings = base.appendingPathComponent("recordings", isDirectory: true)
        try FileManager.default.createDirectory(at: recordings, withIntermediateDirectories: true)
        XCTAssertTrue(store.hasPriorInstallEvidence())
        try FileManager.default.removeItem(at: recordings)

        let config = base.appendingPathComponent("config.toml")
        try Data().write(to: config)
        XCTAssertTrue(store.hasPriorInstallEvidence())
        try FileManager.default.removeItem(at: config)

        let plist = agents.appendingPathComponent("com.screencap.daemon.plist")
        try Data().write(to: plist)
        XCTAssertTrue(store.hasPriorInstallEvidence())
    }

    func testCompletionMarkerRoundTripAndIdempotence() throws {
        let base = try makeTempDir()
        let store = OnboardingMarkerStore(
            baseDirectory: base, launchAgentsDirectory: base,
            defaults: UserDefaults(suiteName: "onboarding-tests-\(UUID().uuidString)")!
        )
        XCTAssertFalse(store.isCompleted())
        try store.markCompleted()
        XCTAssertTrue(store.isCompleted())
        // Write-once semantics are idempotent, not error-on-repeat.
        XCTAssertNoThrow(try store.markCompleted())
        XCTAssertTrue(store.isCompleted())
    }
}
