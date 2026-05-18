import SwiftUI

/// Pure derivation of the per-row badge label, toggle state, and tooltip from
/// an `InstalledApp`. Separated from `PrivacyAppRow` so the matrix-resolution
/// logic — the part of the pane most likely to drift if the Python side
/// changes — can be exercised without spinning up a SwiftUI render tree.
///
/// Priority order, top wins:
///   1. `is_matrix_exclude` — always blocked, regardless of user overrides.
///   2. `in_exclude_apps`   — user explicitly excluded.
///   3. `in_allow_apps`     — user explicitly allowed (when not blocked above).
///   4. `resolved_action`   — fall back to the matrix-resolved action.
///
/// The `*` decoration on an `allow`-resolved row signals that
/// `mask_domains` / `mask_title_patterns` rules exist at the config level
/// and may still mask this app at capture time. We decorate only `allow`
/// rows (matrix-masked rows already disclose the masking).
struct PrivacyBadgeStyle: Equatable {
    let text: String
    let toggleDisabled: Bool
    let toggleOn: Bool
    let tooltip: String?
    let kind: Kind

    enum Kind: Equatable {
        case blockedBySecurity
        case excludedByUser
        case captured
    }

    var color: Color {
        switch kind {
        case .blockedBySecurity: return .red
        case .excludedByUser:    return .orange
        case .captured:          return .secondary
        }
    }

    static func derive(for app: InstalledApp) -> PrivacyBadgeStyle {
        if app.isMatrixExclude {
            return PrivacyBadgeStyle(
                text: "Always blocked (security)",
                toggleDisabled: true,
                toggleOn: true,
                tooltip: "Blocked under every privacy mode for security. This setting can't be changed.",
                kind: .blockedBySecurity
            )
        }
        if app.inExcludeApps {
            return PrivacyBadgeStyle(
                text: "Excluded by you",
                toggleDisabled: false,
                toggleOn: true,
                tooltip: nil,
                kind: .excludedByUser
            )
        }

        let baseText: String
        if app.inAllowApps && app.resolvedAction != "exclude" {
            baseText = "Captured (allowed by you)"
        } else {
            switch app.resolvedAction {
            case "mask_window":
                baseText = "Captured (window masked)"
            case "text_redact":
                baseText = "Captured (text redacted)"
            case "allow":
                baseText = "Captured"
            default:
                // Defensive: an unknown action from a newer CLI should still
                // render as "captured" rather than blank-out the row.
                baseText = "Captured"
            }
        }

        let isAllow = app.resolvedAction == "allow"
        let decorate = isAllow && app.hasPerFrameOverrides
        let text = decorate ? "\(baseText)*" : baseText
        let tooltip = decorate
            ? "Domain or window-title rules in your privacy config may still mask this app at capture time."
            : nil
        return PrivacyBadgeStyle(
            text: text,
            toggleDisabled: false,
            toggleOn: false,
            tooltip: tooltip,
            kind: .captured
        )
    }
}
