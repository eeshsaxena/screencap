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
}

/// The pane's honesty-gated copy (KTD-9): the E2EE row is informational with
/// no active claim (SCR-220), the private-window row states the capability
/// does not exist yet (SCR-224), and the mask row's "always on" describes the
/// capture-time policy engine, which genuinely always runs.
/// PrivacySettingsPolicyTests string-asserts these.
enum PrivacySettingsCopy {
    static let paneTitle = "Privacy"
    static let paneSub = "Where your recordings live and what leaves this Mac."

    static let keepLocalTitle = "Keep recordings local by default"

    // Stub: SCR-220 end-to-end encryption for shared copies — informational
    // row, no active toggle, no "always on" claim (KTD-9).
    static let e2eeTitle = "End-to-end encryption for shared copies"
    static let e2eeChip = "planned"
    static let e2eeSub = "Not available yet. Sharing today uses per-recording upload approval instead."
    static let e2eeHelp = "Coming soon — SCR-220"

    // The always-on capture-time policy engine (true today); per-app
    // overrides are SCR-225.
    static let maskTitle = "Mask sensitive content automatically"
    static let maskChip = "always on"
    static let maskSub = "Policy-driven: password managers, banking, and other sensitive windows are masked or blocked while recording."
    static let maskLink = "See per-app rules"
    static let maskHelp = "Masking is policy-driven and always on. Per-app overrides coming soon — SCR-225"

    // Stub: SCR-224 private-window detection / auto-pause — the design claims
    // auto-pause exists; it does not, so the row says so (R7).
    static let pauseTitle = "Pause when a private window is focused"
    static let pauseSub = "Not available yet — recording does not auto-pause over private windows today."
    static let pauseHelp = "Coming soon — SCR-224"

    // Stub: SCR-228 change storage location with migration.
    static let storageTitle = "Storage location"
    static let storageChangeLabel = "Change…"
    static let storageChangeHelp = "Coming soon — SCR-228"

    /// Every row string, for the KTD-9 gate: no active-encryption claims while
    /// SCR-220 is open, no auto-pause claims while SCR-224 is open.
    static var allRowStrings: [String] {
        [
            keepLocalTitle,
            e2eeTitle, e2eeChip, e2eeSub,
            maskTitle, maskChip, maskSub, maskLink,
            pauseTitle, pauseSub,
            storageTitle, storageChangeLabel,
        ]
    }
}
