import Foundation

/// U13 — pure mapping from an `apps --json` row to the App-rules segmented
/// control: which of Record / Mask / Block is selected, which segments accept
/// interaction, the mono note under the app name, and the CLI transition a tap
/// issues. Resolution priority (SCR-235 — the matrix-immutable hard lock is
/// retired; a confirmed allow is authoritative and sensitive classes unlock
/// behind a one-time confirmation):
///
///   1. `in_exclude_apps`   — Block selected; Record un-blocks.
///   2. `allow_confirmed`   — Record selected by the user's confirmed allow.
///   3. `resolved_action == exclude` — Block selected (matrix default for
///      sensitive classes at the current mode); Record either confirms
///      (`confirmation_required`) or attempts the allow override.
///   4. `resolved_action`   — matrix-resolved: mask → Mask selected-but-locked
///      (the per-app Mask *override* is SCR-225; the matrix mask itself is
///      real), allow → Record. A legacy (unconfirmed) allow entry renders its
///      real floor state here; Record re-confirms it.
///
/// The Mask segment is never tappable in v1: it is either the matrix's own
/// (true) state or a stub (KTD-8, SCR-225).
struct AppRuleSegmentPolicy: Equatable {
    enum Segment: Equatable {
        case record
        case mask
        case block
    }

    /// The CLI write a segment tap issues, executed via the existing
    /// `PrivacyController` mutators (R8).
    enum Transition: Equatable {
        case excludeAdd
        case excludeRemove
        case allowAdd
        /// SCR-235: allow a confirmation-required app. The view shows the
        /// consequences dialog first; on confirm the CLI runs with the
        /// `--confirm-sensitive` flag.
        case allowConfirm
    }

    /// The selected segment; nil renders no selection (the SCR-224 stub row).
    let selection: Segment?
    let recordEnabled: Bool
    let blockEnabled: Bool
    /// Tooltip explaining a fully-locked row; nil for writable rows.
    /// SCR-235 retired the matrix-immutable lock, so this is nil for every
    /// current row shape; kept for future locked states (e.g. SCR-224).
    let lockedReason: String?
    let note: String

    /// Tooltip on the (never-tappable) Mask segment when it is not the
    /// matrix-selected state.
    static let maskStubHelp = "Coming soon — SCR-225"

    static func derive(for app: InstalledApp) -> AppRuleSegmentPolicy {
        let classLabel = contextClassLabel(app.contextClass)

        if app.inExcludeApps {
            return AppRuleSegmentPolicy(
                selection: .block,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: "blocked by you"
            )
        }
        if app.allowConfirmed {
            return AppRuleSegmentPolicy(
                selection: .record,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: "recorded · allowed by you"
            )
        }
        if app.resolvedAction == "exclude" {
            // Matrix-resolved EXCLUDE without the user's own exclude entry —
            // the sensitive-class default (SCR-235 made this a first-class,
            // unlockable state; it was the hard-locked matrix-immutable row).
            // Record confirms the allow (consequences dialog) when the class
            // requires it, or attempts the plain allow override otherwise.
            return AppRuleSegmentPolicy(
                selection: .block,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: joined("blocked by default", classLabel)
            )
        }
        switch app.resolvedAction {
        case "mask_window":
            return AppRuleSegmentPolicy(
                selection: .mask,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: joined("window masked while recording", classLabel)
            )
        case "text_redact":
            return AppRuleSegmentPolicy(
                selection: .mask,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: joined("text redacted while recording", classLabel)
            )
        default:
            // "allow" and any unknown future action render as recorded rather
            // than blanking the row.
            return AppRuleSegmentPolicy(
                selection: .record,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: app.inAllowApps ? "recorded · allowed by you" : "recorded"
            )
        }
    }

    /// The CLI transition for tapping `segment` on `app`; nil is a no-op
    /// (already selected or the never-writable Mask segment).
    static func transition(for app: InstalledApp, tapping segment: Segment) -> Transition? {
        switch segment {
        case .mask:
            return nil
        case .block:
            let current = derive(for: app)
            return current.selection == .block ? nil : .excludeAdd
        case .record:
            if app.inExcludeApps { return .excludeRemove }
            let current = derive(for: app)
            if current.selection == .record { return nil }
            // Unlocking a confirmation-required class goes through the
            // consequences dialog (SCR-235); everything else is a plain
            // allow write (which the CLI records as confirmed, and which
            // re-confirms a legacy entry).
            if app.confirmationRequired && !app.allowConfirmed {
                return .allowConfirm
            }
            return .allowAdd
        }
    }

    /// The consequences message for the SCR-235 confirmation dialog. Names
    /// the full cascade (R7): raw capture, keystrokes, the search index
    /// (including already-recorded frames once indexing re-runs), and cloud
    /// copies — phrased conditionally, since the destination is often decided
    /// per recording.
    static func confirmationMessage(for app: InstalledApp) -> String {
        let label = contextClassLabel(app.contextClass).map { " (\($0))" } ?? ""
        return "“\(app.displayName)”\(label) will be fully recordable in every "
            + "privacy mode: screen and video captured raw, typed text kept, and "
            + "its content searchable — including frames from past recordings "
            + "once the search index re-runs. If a recording is sent to the "
            + "cloud, this app is included. You can block it again at any time."
    }

    /// Human label for a `context_class` value (nil for `unknown`, so the note
    /// never reads "· unknown").
    static func contextClassLabel(_ contextClass: String) -> String? {
        switch contextClass {
        case "password_manager": return "password manager"
        case "banking": return "banking"
        case "email": return "email"
        case "chat": return "chat"
        case "calendar": return "calendar"
        case "video_call": return "video calls"
        case "browser_unverified": return "browser"
        case "code_editor_terminal": return "code editor"
        case "admin_console": return "admin console"
        case "auth_flow": return "sign-in flow"
        case "payment_flow": return "payments"
        case "cloud_storage": return "cloud storage"
        default: return nil
        }
    }

    private static func joined(_ base: String, _ classLabel: String?) -> String {
        guard let classLabel else { return base }
        return "\(base) · \(classLabel)"
    }
}
