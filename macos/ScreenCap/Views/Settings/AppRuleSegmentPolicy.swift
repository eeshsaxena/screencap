import Foundation

/// U13 — pure mapping from an `apps --json` row to the App-rules segmented
/// control: which of Record / Mask / Block is selected, which segments accept
/// interaction, the mono note under the app name, and the CLI transition a tap
/// issues. Resolution priority (inherited from the retired Privacy pane's
/// `PrivacyBadgeStyle.derive`, now the single owner):
///
///   1. `is_matrix_exclude` — always blocked; the whole row is locked.
///   2. `in_exclude_apps`   — Block selected; Record un-blocks.
///   3. `resolved_action == exclude` (defensive, hand-edited config).
///   4. `in_allow_apps`     — Record selected by the user's own override.
///   5. `resolved_action`   — matrix-resolved: mask → Mask selected-but-locked
///      (the per-app Mask *override* is SCR-225; the matrix mask itself is
///      real), allow → Record.
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
    }

    /// The selected segment; nil renders no selection (the SCR-224 stub row).
    let selection: Segment?
    let recordEnabled: Bool
    let blockEnabled: Bool
    /// Tooltip explaining a fully-locked row; nil for writable rows.
    let lockedReason: String?
    let note: String

    /// Tooltip on the (never-tappable) Mask segment when it is not the
    /// matrix-selected state.
    static let maskStubHelp = "Coming soon — SCR-225"

    static func derive(for app: InstalledApp) -> AppRuleSegmentPolicy {
        let classLabel = contextClassLabel(app.contextClass)

        if app.isMatrixExclude {
            return AppRuleSegmentPolicy(
                selection: .block,
                recordEnabled: false,
                blockEnabled: false,
                lockedReason: "Blocked under every privacy mode for security. This can't be changed.",
                note: joined("blocked by default", classLabel)
            )
        }
        if app.inExcludeApps {
            return AppRuleSegmentPolicy(
                selection: .block,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: "blocked by you"
            )
        }
        if app.resolvedAction == "exclude" {
            // Matrix-resolved EXCLUDE without the user's own exclude entry —
            // only reachable via a hand-edited config (the CLI guard refuses
            // the allow write that would produce it). Render honestly as
            // blocked; Record attempts the allow override and surfaces the
            // CLI's verdict.
            return AppRuleSegmentPolicy(
                selection: .block,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: joined("blocked", classLabel)
            )
        }
        if app.inAllowApps {
            return AppRuleSegmentPolicy(
                selection: .record,
                recordEnabled: true,
                blockEnabled: true,
                lockedReason: nil,
                note: "recorded · allowed by you"
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
                note: "recorded"
            )
        }
    }

    /// The CLI transition for tapping `segment` on `app`; nil is a no-op
    /// (already selected, locked row, or the never-writable Mask segment).
    static func transition(for app: InstalledApp, tapping segment: Segment) -> Transition? {
        guard !app.isMatrixExclude else { return nil }
        switch segment {
        case .mask:
            return nil
        case .block:
            let current = derive(for: app)
            return current.selection == .block ? nil : .excludeAdd
        case .record:
            if app.inExcludeApps { return .excludeRemove }
            let current = derive(for: app)
            switch current.selection {
            case .record:
                return nil
            case .block:
                // Defensive resolved-exclude state: attempt the allow override;
                // the CLI's matrix guard is the authority and its refusal
                // surfaces as the row's error.
                return .allowAdd
            case .mask:
                return .allowAdd
            case nil:
                return nil
            }
        }
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
