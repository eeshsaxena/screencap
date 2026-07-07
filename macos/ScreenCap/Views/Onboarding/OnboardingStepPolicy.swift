import Foundation

// U11 — the onboarding wizard's pure decision layer (KTD-10). The current step
// is always *derived* from persisted markers + live daemon grants — never a
// stored step integer — so a Quit & Relaunch mid-wizard lands back on the right
// step. All copy that the honesty gates (KTD-9 / R7) constrain lives here as
// constants so OnboardingStepPolicyTests can string-assert it without a render
// tree.

/// The wizard's six screens (design 50–294). Raw values match the design's
/// `obStep` indices and drive the progress-dot highlight.
enum OnboardingStep: Int, Equatable, CaseIterable {
    case welcome = 0
    case permissions = 1
    case appRules = 2
    case storage = 3
    case account = 4
    case teamSetup = 5
    /// SCR-239 — the opt-in downloadable-model offer. Local tier only, shown
    /// after `storage` (the local tier has no account/team steps, so it is the
    /// local tier's 5th dot). Raw value 6 keeps the design's 0–5 indices stable.
    case downloadModel = 6
}

/// The storage tier picked on step 3. Cloud tiers extend the wizard with the
/// account (and, for team, team-setup) steps; the dot count follows (KTD-10).
enum OnboardingStorageTier: Equatable {
    case local
    case personalCloud
    case teamCloud

    /// Seed the replay wizard's selection from the persisted `upload_default`
    /// (there is no persisted "tier"; the upload default is its durable trace).
    static func from(uploadDefault: String?) -> OnboardingStorageTier {
        switch uploadDefault {
        case "cloud", "both": return .personalCloud
        default: return .local
        }
    }
}

enum OnboardingStepPolicy {

    // MARK: - Takeover decision (extends the first-run presentation machinery)

    /// What the main window should do about onboarding at launch (KTD-10).
    enum Takeover: Equatable {
        /// Fresh install (or a wizard already in progress): replace the window
        /// content with the wizard. The caller persists the pending flag.
        case wizard
        /// Prior-install evidence with no marker: backfill the completion
        /// marker write-once and show the normal shell. Existing users never
        /// see the marketing wizard.
        case backfillMarker
        /// Normal shell (completed, skipped, or recording in flight).
        case none
    }

    /// Pure takeover decision. `wizardPending` — not fresh evidence — keeps a
    /// mid-wizard machine in the wizard across relaunches: the app's own
    /// first-launch writes (`config.toml` via `ensureFirstLaunchModeWritten`)
    /// would otherwise read as prior-install evidence on launch 2 and strand a
    /// genuinely-fresh user out of their half-finished wizard.
    ///
    /// `setupSkipped` is the persisted "Skip for now" (the same
    /// `PermissionController.setupDismissed` flag the walkthrough sheet uses),
    /// so a skip stops the takeover re-popping while the recovery latch stays
    /// available. Migration is deliberately NOT an input: upgrade users carry
    /// prior-install evidence, land in `.backfillMarker`, and keep the existing
    /// migration-interstitial sheet flow (KTD-10).
    static func takeover(
        completed: Bool,
        wizardPending: Bool,
        priorInstallEvidence: Bool,
        setupSkipped: Bool,
        isRecording: Bool
    ) -> Takeover {
        guard !isRecording else { return .none }
        if completed { return .none }
        if wizardPending, !setupSkipped { return .wizard }
        if priorInstallEvidence { return .backfillMarker }
        if setupSkipped { return .none }
        return .wizard
    }

    // MARK: - Step derivation (KTD-10: derived, never stored)

    /// The step the wizard (re)opens on. Before the welcome CTA is tapped the
    /// wizard always starts at welcome; after it, a relaunch lands on
    /// permissions until the daemon confirms the required grants, then app
    /// rules — never back at welcome. Indeterminate grants are NOT granted
    /// (the user can still Continue past the permissions step, so
    /// indeterminate never blocks — it just doesn't skip the step).
    static func initialStep(
        wizardStarted: Bool,
        requiredGrantsGranted: Bool
    ) -> OnboardingStep {
        guard wizardStarted else { return .welcome }
        return requiredGrantsGranted ? .appRules : .permissions
    }

    /// Auto-advance analog of `shouldAutoCloseOnUpdate`: while the permissions
    /// step is up and the daemon confirms every required grant, advance to app
    /// rules without a click. Strictly positive — indeterminate never
    /// auto-advances (a transient probe hiccup must not skip the step the user
    /// is reading).
    static func shouldAutoAdvance(
        from step: OnboardingStep,
        requiredGrantsGranted: Bool
    ) -> Bool {
        step == .permissions && requiredGrantsGranted
    }

    // MARK: - Helper install auto-start (dead-registration self-heal)

    /// Whether the permissions surface should drive `DaemonInstallController
    /// .install()` on appear, without waiting for an "Approve helper" click.
    ///
    /// SMAppService records the helper's absolute executable path at
    /// registration time. When that bundle later disappears (a deleted dev
    /// worktree's Debug build, or an app moved after first launch), the label
    /// still reads `.enabled` while launchd sits in `spawn failed` — so the
    /// daemon can never come up, every grant reads indeterminate, and the
    /// idle card's "approve it first" copy dead-ends the user: Login Items
    /// already shows the helper approved. `install()` is the only repair
    /// (its poll-timeout → registration-refresh path re-registers from the
    /// current bundle), so the surface fires it itself.
    ///
    /// Strictly `.idle`: in-flight states must not double-fire, and failure
    /// states keep their explicit user-driven retry (no auto-retry loop). A
    /// connected daemon transport means nothing needs installing, and replay
    /// is read-only by contract.
    ///
    /// `daemonProbeCompleted` gates the launch race: `transport` starts as
    /// `.cliFallback` before the launch probe lands, and unlike the takeover
    /// (gated in MainWindow on this same flag) the wizard path can render the
    /// permissions step pre-probe — without this input the auto-fire would
    /// always poll an as-yet-unprobed socket and could escalate a healthy
    /// daemon into `install()`'s destructive re-register. The step re-evaluates
    /// on the flag's change, so a step that appeared pre-probe still
    /// self-heals the moment the probe settles.
    static func shouldAutoStartHelperInstall(
        installerState: DaemonInstallController.State,
        transport: RecorderTransport,
        daemonProbeCompleted: Bool,
        replay: Bool
    ) -> Bool {
        installerState == .idle
            && transport != .daemon
            && daemonProbeCompleted
            && !replay
    }

    // MARK: - Progress dots (design 289–294; logic obDots)

    /// 5 dots for the local tier (welcome, permissions, appRules, storage,
    /// download-model — SCR-239), 5 with the account step (personal), 6 with
    /// team setup.
    static func dotCount(tier: OnboardingStorageTier) -> Int {
        switch tier {
        case .local: return 5
        case .personalCloud: return 5
        case .teamCloud: return 6
        }
    }

    static func activeDotIndex(for step: OnboardingStep) -> Int {
        // The download-model step is the local tier's 5th dot (index 4); the
        // local tier never shows account(4)/teamSetup(5), so there's no collision.
        step == .downloadModel ? 4 : step.rawValue
    }

    // MARK: - Storage routing (logic storageNext / accountNext)

    /// The step after the storage CTA: local continues to the SCR-239
    /// download-model offer, cloud tiers continue to the account step.
    static func stepAfterStorage(tier: OnboardingStorageTier) -> OnboardingStep? {
        tier == .local ? .downloadModel : .account
    }

    /// The step after a successful sign-in: team continues to team setup,
    /// personal finishes.
    static func stepAfterAccount(tier: OnboardingStorageTier) -> OnboardingStep? {
        tier == .teamCloud ? .teamSetup : nil
    }

    static func storageCTATitle(tier: OnboardingStorageTier) -> String {
        switch tier {
        case .local: return "Keep it on this Mac"
        case .personalCloud: return "Continue — create your account"
        case .teamCloud: return "Continue — set up your team"
        }
    }
}

/// Honesty-gated copy (KTD-9 / R7): no pricing, billing, encryption, or
/// team-sharing claims until SCR-220 / SCR-221 / SCR-229 land. String-level
/// assertions in OnboardingStepPolicyTests pin these.
enum OnboardingCopy {

    // Step 0 — welcome (design 57–85, with the KTD-9 substitutions: the
    // design's "Encrypted sharing … Always." card is untrue until SCR-220, and
    // "Recordings carry context from the tools you use" is SCR-227; both are
    // replaced with what is true today).
    static let welcomeHeadline = "Teach it the way you'd show a friend."
    static let welcomeSub =
        "Screencap records your screen so new teammates learn from real work "
        + "— not documentation that's already out of date."
    static let welcomeCards: [(title: String, body: String)] = [
        ("Local by default", "Everything stays on this Mac until you share it."),
        ("You choose what leaves", "Uploads happen only when you approve them, recording by recording."),
        ("Works with your agents", "MCP-connected agents can search what you've recorded."),
    ]

    // Step 1 — permissions (design 87–151).
    static let permissionsHeadline = "Let Screencap see your screen."
    static let permissionsSub =
        "macOS asks once. Recordings still stay on this Mac — permissions only "
        + "let the app capture, not upload."

    // Step 2 — app rules (design 153–194). The design's footer claim "Private
    // browser windows always pause recording" is SCR-224 and must not ship
    // (R7); the surviving footer only states what is true.
    static let appRulesHeadline = "Some things are nobody's business."
    static let appRulesSub =
        "We've pre-blocked the obvious ones. Recording skips blocked apps "
        + "automatically — the video just cuts around them."
    static let appRulesFooter = "Fine-tune per app anytime in Settings → App rules."

    // Step 3 — storage (design 196–236). The local card's design copy is true
    // and ships as-is except the "Share by exporting encrypted files" bullet
    // (SCR-220). The cloud cards drop pricing (KTD-9) and carry only what is
    // true today: per-recording upload approved in the Review window and
    // single-recording web sharing; future capability is future-tense.
    static let storageHeadline = "Where should your recordings live?"
    static let storageSub =
        "Either way, recording happens on this Mac. This only decides what happens after."
    static let storageFootnote =
        "You can switch anytime in Settings → Privacy. Local files stay local when you upgrade."

    static let localCardTitle = "This Mac only"
    static let localCardMeta = "free · forever · no account"
    static let localCardBullets = [
        "Everything stays on your disk",
        "Upload only what you approve in Review",
        "Nothing touches our servers until you share",
    ]

    static let personalCardTitle = "Personal cloud"
    static let personalCardMeta = "$5/month"
    static let personalCardBullets = [
        "Upload the recordings you approve",
        "Share single recordings by link",
        "Cross-Mac backup is on the way",
    ]

    static let teamCardTitle = "Team cloud"
    static let teamCardMeta = "you + your team"
    static let teamCardBullets = [
        "Upload the recordings you approve",
        "Share single recordings by link",
        "Team libraries and invites are on the way",
    ]

    // Step 4 — account (design 238–262). The design's "keys and billing" copy
    // and the "never readable by us" footer are SCR-221/SCR-229/SCR-220 scope
    // (KTD-9) — the interim copy claims nothing about keys, billing, or
    // encryption.
    static let accountHeadline = "Create your Screencap account."
    static let accountSub =
        "Sign-in opens in your browser. Uploads still happen only when you "
        + "approve them, recording by recording."

    // Billing U9 — the post-sign-in upgrade panel for Personal cloud. Unlike the
    // pre-billing gate, these DO carry the price ($5/mo is true and billable
    // now), but carry NO end-to-end-encryption / "we can't watch" claim (R2 —
    // the paid tier is server-readable). Team stays price-free (coming soon).
    static let upgradeSub =
        "Personal cloud keeps the recordings you approve backed up and shareable. "
        + "Local recording stays free either way."
    static let upgradePriceLine = "$5 / month · cancel anytime"
    static let upgradeBullets = [
        "Upload the recordings you approve",
        "Share single recordings by link",
    ]

    // Step 5 — team setup (design 264–288). The design's "one shared,
    // encrypted library" is SCR-221/SCR-220; the fields render per the design
    // but disabled (KTD-8) under future-tense copy.
    static let teamHeadline = "Set up your team."
    static let teamSub = "Team libraries are on the way — you'll be the admin when they land."

    /// Billing U10 — Team cloud is coming-soon; capture interest via a hosted
    /// waitlist form (no team backend yet, KTD-7). Swap for a self-hosted
    /// endpoint later. Placeholder URL — replace with the real form before launch.
    static let teamWaitlistURL = "https://screencap.app/team-waitlist"

    // SCR-239 download-model step (local tier only). Opt-in, non-blocking — Skip
    // leaves the user on the idle-gap heuristic (R7); nothing here claims cloud
    // quality or blocks recording.
    static let downloadModelHeadline = "Want named tasks on this Mac?"
    static let downloadModelSub =
        "Download a small model (about 2 GB) and Screencap names your day's tasks "
        + "on-device — nothing leaves the Mac. You can skip this and add it later "
        + "in Settings → Intelligence."
    static let downloadModelSkip = "Not now"

    /// Every string the storage + account steps render, for the KTD-9
    /// string-level gate (no pricing, encryption, or team-sharing claims).
    static var storageAndAccountStrings: [String] {
        [
            storageHeadline, storageSub, storageFootnote,
            localCardTitle, localCardMeta,
            personalCardTitle, personalCardMeta,
            teamCardTitle, teamCardMeta,
            accountHeadline, accountSub,
            OnboardingStepPolicy.storageCTATitle(tier: .local),
            OnboardingStepPolicy.storageCTATitle(tier: .personalCloud),
            OnboardingStepPolicy.storageCTATitle(tier: .teamCloud),
        ]
        + localCardBullets + personalCardBullets + teamCardBullets
    }
}
