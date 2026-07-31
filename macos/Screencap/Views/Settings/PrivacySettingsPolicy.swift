import Foundation

// U12 — the Privacy settings pane's pure decision layer + honesty-gated copy
// (KTD-9 / KTD-11 / R7). Kept out of the view so the keep-local toggle rules
// and the no-untrue-claims strings are assertable without a render tree
// (PrivacySettingsPolicyTests).

enum PrivacySettingsPolicy {

    /// KTD-11: the keep-local toggle is ON iff `upload_default == "local"`.
    /// `ask` / `cloud` / `both` (and unknown/nil) all render OFF — the caption
    /// names the actual default so a non-local value is never misread as
    /// "local".
    static func keepLocalToggleOn(uploadDefault: String?) -> Bool {
        uploadDefault == "local"
    }

    /// The caption under the keep-local row, naming the current default
    /// (mirrors U6's header treatment). This is what makes an ON→OFF write of
    /// `ask` visible, so a `cloud`/`both` default is never silently clobbered.
    static func keepLocalCaption(uploadDefault: String?) -> String {
        switch uploadDefault {
        case "local":
            return "New recordings never upload unless you share them."
        case "ask":
            return "Current default: ask — each upload needs your approval."
        case "cloud":
            return "Current default: cloud — new recordings upload after processing."
        case "both":
            return "Current default: both — recordings are kept here and uploaded."
        default:
            return "Current default: unknown — couldn't read your settings."
        }
    }

    /// KTD-11: turning the toggle ON writes `local`; turning it OFF writes
    /// `ask` (never `cloud`/`both` — OFF only ever restores the per-recording
    /// consent default).
    static func uploadDefaultValue(togglingTo on: Bool) -> String {
        on ? "local" : "ask"
    }

    // MARK: - E2EE row (SCR-220 U4, SCR-260 cloud-capability gate)

    /// Whether the user can actually use E2EE for cloud copies (SCR-260). Cloud
    /// copies exist only for a signed-in, cloud-capable user, so the off→on
    /// enable path — which mints a synchronizable iCloud-Keychain KEK — is gated
    /// on this. `eligible` keeps the shipped SCR-220 flow; the other three are
    /// gated states, each with its own honest caption.
    enum E2EECloudEligibility: Equatable {
        /// Signed in with cloud upload capability — a Cloud subscriber/trialist,
        /// or any signed-in user on a paywall-off (pre-billing / dev) build.
        case eligible
        /// Not signed in — no account, so no cloud upload and no cloud copies.
        case signedOut
        /// Signed in without a cloud-capable plan (Local Pro, or lapsed).
        case noCloudPlan
        /// Signed in but offline/stale, so the plan can't be positively
        /// confirmed — gated fail-closed (never mint a KEK on an unconfirmed
        /// plan), but told apart from `noCloudPlan` so an offline Cloud payer is
        /// never shown an "upgrade" prompt (mirrors the billing plan's grace).
        case planUnconfirmed
    }

    /// Resolve E2EE cloud-eligibility from the auth signals the pane reads
    /// (SCR-260). `cloudCapable` is the app's positive cloud signal
    /// (`CloudAuthController.isSubscribed`, i.e. `tier == .cloud`, true during a
    /// Cloud trial). The `!paywallEnabled` branch keeps pre-billing / dev builds
    /// working: there `tier` is `.none` for everyone, so without it every
    /// signed-in user would be gated. `planStale` (offline) is fail-closed but
    /// kept distinct so an offline payer isn't told to upgrade.
    static func e2eeEligibility(
        isSignedIn: Bool,
        cloudCapable: Bool,
        planStale: Bool,
        paywallEnabled: Bool
    ) -> E2EECloudEligibility {
        guard isSignedIn else { return .signedOut }
        if !paywallEnabled { return .eligible }
        if cloudCapable { return .eligible }
        if planStale { return .planUnconfirmed }
        return .noCloudPlan
    }

    /// What a tap on the E2EE toggle does, keyed on the runtime flag state and
    /// (for the off→on path) cloud-eligibility.
    /// `locked`: nil flag (older CLI without the `e2ee` verb, KTD-8) — the row
    /// is non-interactive, regardless of eligibility. `showDisclosure`: the
    /// off→on tap by an eligible user presents the R4 limits sheet INSTEAD of
    /// flipping — the switch stays OFF and no CLI call fires until the user
    /// confirms (cancel = dismiss, nothing else). `gated` (SCR-260): the off→on
    /// tap by a non-cloud-capable user — non-interactive, no disclosure, no
    /// KEK-creating write. `disable`: the on→off tap needs no disclosure and is
    /// eligibility-independent (turning encryption off is always harmless, so a
    /// since-lapsed user can still do it — R4).
    enum E2EETapOutcome: Equatable {
        case locked
        case showDisclosure
        case disable
        case gated
    }

    /// `eligibility` is REQUIRED (no default): this is the security gate, so a
    /// caller must positively supply the auth-derived eligibility. A defaulted
    /// `.eligible` would let a future call site silently un-gate by omission — a
    /// compile error is the wanted failure mode here. (`e2eeCaption`/`e2eeHelp`
    /// default to `.eligible` because they only pick copy, not the gate.)
    static func e2eeTapOutcome(
        cloudE2EEEnabled: Bool?,
        eligibility: E2EECloudEligibility
    ) -> E2EETapOutcome {
        switch cloudE2EEEnabled {
        case .none: return .locked
        case .some(true): return .disable
        case .some(false): return eligibility == .eligible ? .showDisclosure : .gated
        }
    }

    /// The E2EE toggle renders ON iff the CLI reports the flag true. nil
    /// (unknown) renders OFF — never guess an encryption claim (KD7).
    static func e2eeToggleOn(cloudE2EEEnabled: Bool?) -> Bool {
        cloudE2EEEnabled == true
    }

    /// Chip label: "beta" in both live states (R1 — the opt-in is labeled
    /// beta; no bare capability claim); the stub "planned" chip only when the
    /// flag is unreadable (older CLI, KTD-8).
    static func e2eeChip(cloudE2EEEnabled: Bool?) -> String {
        cloudE2EEEnabled == nil
            ? PrivacySettingsCopy.e2eeChipStub
            : PrivacySettingsCopy.e2eeChipBeta
    }

    /// The caption under the E2EE row, honesty-gated per state (KTD-9/KD7):
    /// nil → the stub copy (no capability claim at all); true → the truthful
    /// scoped beta claim (multi-device encryption active, no recovery); false →
    /// depends on eligibility (SCR-260): the eligible opt-in copy that makes no
    /// claim about current uploads being encrypted, or the gated copy naming why
    /// the row is unavailable (sign in / cloud plan / reconnect). No gated
    /// caption claims current or available encryption, and none says "not
    /// available" (the capability exists — it is gated). Never "always on" or
    /// "shared · encrypted" in any state — those unlock later and
    /// PrivacySettingsPolicyTests string-asserts their absence.
    static func e2eeCaption(
        cloudE2EEEnabled: Bool?,
        eligibility: E2EECloudEligibility = .eligible
    ) -> String {
        switch cloudE2EEEnabled {
        case .none: return PrivacySettingsCopy.e2eeSubStub
        case .some(true): return PrivacySettingsCopy.e2eeSubOn
        case .some(false):
            switch eligibility {
            case .eligible: return PrivacySettingsCopy.e2eeSubOff
            case .signedOut: return PrivacySettingsCopy.e2eeSubGatedSignedOut
            case .noCloudPlan: return PrivacySettingsCopy.e2eeSubGatedNoPlan
            case .planUnconfirmed: return PrivacySettingsCopy.e2eeSubGatedUnconfirmed
            }
        }
    }

    /// Hover help for the E2EE row — conditioned ("when on") so it stays honest
    /// while the toggle is off. nil → stub; a readable-flag row that is gated
    /// (SCR-260, off + non-eligible) → gated help that names the cloud-plan
    /// requirement instead of the "turn on to encrypt" live help, so the tooltip
    /// never contradicts the gated caption.
    static func e2eeHelp(
        cloudE2EEEnabled: Bool?,
        eligibility: E2EECloudEligibility = .eligible
    ) -> String {
        if cloudE2EEEnabled == nil { return PrivacySettingsCopy.e2eeHelpStub }
        if cloudE2EEEnabled == false && eligibility != .eligible {
            return PrivacySettingsCopy.e2eeHelpGated
        }
        return PrivacySettingsCopy.e2eeHelpLive
    }

    /// SCR-228: map a storage-migration refusal `reason` code to honest copy.
    /// The daemon's own `message` is preferred when present; this is the
    /// fallback (older daemon that omits `message`) so the pane never shows a
    /// bare machine code.
    static func migrationFailureFallback(reason: String) -> String {
        switch reason {
        case "cross_volume":
            return "That folder is on a different disk. Moving to an external "
                + "or separate volume isn't supported yet — pick a folder on "
                + "this disk."
        case "cloud_synced":
            return "That folder is synced to iCloud, Dropbox, or another cloud "
                + "service, which would upload your recordings. Choose a folder "
                + "that isn't synced."
        case "target_not_empty":
            return "Choose an empty folder."
        case "same_as_source":
            return "That's already where your recordings are stored."
        case "source_missing":
            return "Your current recordings folder couldn't be found."
        case "not_writable":
            return "That folder isn't writable. Choose another."
        case "nested":
            return "Choose a folder that isn't inside your current recordings "
                + "folder."
        case "recording_active":
            return "Stop the current recording before moving the storage "
                + "location."
        case "migration_in_progress":
            return "A move is already in progress."
        case "env_override":
            return "The storage location is pinned by an environment variable "
                + "and can't be changed here."
        default:
            return "Couldn't move your recordings. Try a different folder."
        }
    }
}

/// The pane's honesty-gated copy (KTD-9/KD7): the E2EE row's copy is keyed on
/// the runtime `cloud_e2ee_enabled` signal (SCR-220 U4 — beta opt-in), the
/// private-window row states the capability does not exist yet (SCR-224),
/// and the mask row's "always on" describes the capture-time policy engine,
/// which genuinely always runs. PrivacySettingsPolicyTests string-asserts
/// these.
enum PrivacySettingsCopy {
    static let paneTitle = "Privacy"
    static let paneSub = "Where your recordings live and what leaves this Mac."

    static let keepLocalTitle = "Keep recordings local by default"

    // SCR-220 U4 / SCR-253 U8: E2EE for cloud copies is a live opt-in beta
    // toggle. All row copy is state-keyed through PrivacySettingsPolicy.e2eeChip/
    // e2eeCaption/e2eeHelp (KD7 — every claim bound to the runtime
    // `cloud_e2ee_enabled` signal): nil (older CLI) keeps the stub presentation
    // below; off makes no claim that current uploads are encrypted; on claims
    // the multi-device beta capability in iCloud-Keychain-honest wording (KTD-6:
    // the key syncs to the user's own Macs, so "neither we nor Apple can read"
    // your recordings — NOT the retired Stage-1 "only this Mac can decrypt")
    // and names the no-recovery limit. Still no "always on" (Stage 3), no
    // "shared · encrypted" (SCR-221), no "keys stay with your team" (KD3).
    static let e2eeTitle = "End-to-end encryption for shared copies"
    static let e2eeChipStub = "planned"
    static let e2eeChipBeta = "beta"
    static let e2eeSubStub = "Not available yet. Sharing today uses per-recording upload approval instead."
    static let e2eeSubOff = "Off — cloud copies upload without end-to-end encryption. Turn on to encrypt future uploads from this Mac."
    static let e2eeSubOn = "On — new cloud copies from this Mac are encrypted. Only your Macs — signed in, with iCloud Keychain on — can decrypt them; neither we nor Apple can read them. No recovery: lose access to all your Macs and you lose access to them."
    static let e2eeHelpStub = "Coming soon — SCR-220"
    static let e2eeHelpLive = "Beta — when on, new cloud copies are encrypted so only your Macs (signed in, with iCloud Keychain on) can decrypt them."

    // SCR-260: the off→on enable path mints a synchronizable KEK, so it is gated
    // on cloud-capability. The gated captions name why the row is unavailable
    // without claiming current/available encryption and without "not available"
    // (the capability exists — it is gated for this user, not absent).
    static let e2eeSubGatedSignedOut = "Cloud copies need a cloud plan. Sign in with a Cloud subscription to encrypt future uploads from this Mac."
    static let e2eeSubGatedNoPlan = "Cloud copies need a cloud plan. Encrypting future uploads is available on the Cloud subscription."
    static let e2eeSubGatedUnconfirmed = "Couldn't confirm your plan while offline. Reconnect to turn on encryption for cloud copies."
    static let e2eeHelpGated = "Encrypting cloud copies is available with a cloud plan."
    // Shown only during the pre-first-check auth window (`.unknown`), so an
    // already-eligible user never flashes the signed-out "sign in" copy.
    static let e2eeSubChecking = "Checking your plan…"

    // R4: the limits disclosure that gates the off→on flip. The body must name
    // the custody and limits plainly BEFORE the user commits (SCR-253 U8,
    // KTD-6): the key syncs to the user's other Macs via iCloud Keychain — so
    // any Mac they're signed into can decrypt, and neither we nor Apple can read
    // it — and there is no recovery, so losing access to all their Macs loses
    // access to the encrypted cloud copies.
    static let e2eeConfirmTitle = "Turn on end-to-end encryption for cloud copies?"
    static let e2eeConfirmBody =
        "This Mac creates the encryption key and syncs it to your other Macs "
        + "through iCloud Keychain, so any Mac where you're signed in with iCloud "
        + "Keychain on can decrypt these cloud copies — but neither we nor Apple "
        + "can read them. There is no recovery: if you lose access to all your "
        + "Macs, you lose access to those encrypted cloud copies. Beta — "
        + "recordings already uploaded are not re-encrypted."
    static let e2eeConfirmAction = "Turn On Encryption"

    // The always-on capture-time policy engine (true today). Per-app Mask
    // rules exist since SCR-225 and tighten it; they cannot switch it off.
    static let maskTitle = "Mask sensitive content automatically"
    static let maskChip = "always on"
    static let maskSub = "Policy-driven: password managers, banking, and other sensitive windows are masked or blocked while recording."
    static let maskLink = "See per-app rules"
    static let maskHelp = "Masking is policy-driven and always on. Set a per-app Mask rule in App rules."

    // Stub: SCR-224 private-window detection / auto-pause — the design claims
    // auto-pause exists; it does not, so the row says so (R7).
    static let pauseTitle = "Pause when a private window is focused"
    static let pauseSub = "Not available yet — recording does not auto-pause over private windows today."
    static let pauseHelp = "Coming soon — SCR-224"

    // SCR-228: change storage location with same-volume migration.
    static let storageTitle = "Storage location"
    static let storageChangeLabel = "Change…"
    static let storageChangeHelp = "Move your recordings to another folder on this disk."
    static let storagePickerMessage = "Choose an empty folder on the same disk for your recordings."
    static let storageConfirmTitle = "Move your recordings?"
    static let storageMigratingLabel = "Moving your recordings…"

    /// Confirmation body — names the destructive/blocking implications before
    /// the move: the whole library relocates, recording is paused, same disk.
    static func storageConfirmBody(target: String) -> String {
        "Your entire recordings library will be moved to \(target). "
            + "Recording is paused during the move, and the new folder must be "
            + "on the same disk."
    }

    /// Every row string for a given E2EE state, for the KTD-9/KD7 gate: the
    /// E2EE strings are state-keyed, so the gate sweeps all three flag states —
    /// plus the SCR-260 gated captions and gated help, which are eligibility-
    /// keyed rather than flag-keyed and so are included unconditionally — no
    /// auto-pause claims while SCR-224 is open, and no later-stage E2EE claims
    /// in any state.
    static func allRowStrings(cloudE2EEEnabled: Bool?) -> [String] {
        [
            keepLocalTitle,
            e2eeTitle,
            PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: cloudE2EEEnabled),
            PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: cloudE2EEEnabled),
            PrivacySettingsPolicy.e2eeHelp(cloudE2EEEnabled: cloudE2EEEnabled),
            e2eeSubGatedSignedOut, e2eeSubGatedNoPlan, e2eeSubGatedUnconfirmed,
            e2eeHelpGated, e2eeSubChecking,
            e2eeConfirmTitle, e2eeConfirmBody, e2eeConfirmAction,
            maskTitle, maskChip, maskSub, maskLink,
            pauseTitle, pauseSub,
            storageTitle, storageChangeLabel,
        ]
    }
}
