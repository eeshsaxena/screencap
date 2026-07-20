import SwiftUI

// Design-system building blocks for the permissions screen
// (`OnboardingPermissionsStep`, design 87–151), which serves both the
// onboarding wizard (U11) and the permission-repair takeover
// (`PermissionSetupTakeover`, U14).

/// The warm pill chrome shared by permission rows and the install card
/// (design 94–120): paper fill, warm hairline border, chip radius.
struct PermissionPillChrome: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 16)
            .padding(.vertical, 13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
    }
}

extension View {
    func permissionPillChrome() -> some View {
        modifier(PermissionPillChrome())
    }
}

/// Capsule-outline action button — the compact teal action used inside the
/// permission pills.
struct PermissionCapsuleButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scTeal)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .overlay(Capsule().strokeBorder(Color.scTeal.opacity(0.5), lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// Trailing grant badge: filled teal check when granted, hollow circle when
/// not. The accessibility label is caller-supplied so the tri-state honesty
/// rule survives — an ungranted state never reads "Granted" (the row's mono
/// status line carries the honest tri-state wording).
struct GrantStateBadge: View {
    let granted: Bool
    let accessibilityLabel: String

    var body: some View {
        Group {
            if granted {
                Circle()
                    .fill(Color.scTeal)
                    .frame(width: 20, height: 20)
                    .overlay(
                        Image(systemName: "checkmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(Color.scCanvas)
                    )
            } else {
                Circle()
                    .strokeBorder(Color.scBorderWarm, lineWidth: 2)
                    .frame(width: 20, height: 20)
            }
        }
        .accessibilityLabel(accessibilityLabel)
    }
}

/// The helper-install card: title + live mono status line + capsule actions,
/// driven entirely by `DaemonInstallController`. The wizard's permissions step
/// and the walkthrough sheet embed the same card, so the "approve the helper"
/// moment looks identical wherever the user meets it.
struct HelperInstallCard: View {
    @ObservedObject var daemonInstaller: DaemonInstallController

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Screencap helper")
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(Self.statusText(for: daemonInstaller.state))
                    .font(SCTypography.mono(size: 10.5))
                    .foregroundStyle(Self.statusColor(for: daemonInstaller.state))
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                switch daemonInstaller.state {
                case .idle, .installFailed, .pollingFailed:
                    PermissionCapsuleButton(title: "Approve helper") {
                        Task { await daemonInstaller.install() }
                    }
                case .requiresApproval:
                    PermissionCapsuleButton(title: "Open Login Items") {
                        DaemonInstallController.openLoginItemsSettings()
                    }
                    PermissionCapsuleButton(title: "Retry") {
                        Task { await daemonInstaller.retry() }
                    }
                case .registering, .polling:
                    ProgressView().controlSize(.small)
                case .installedAndRunning:
                    EmptyView()
                }
            }
        }
        .permissionPillChrome()
    }

    /// The mono status line. Failure reasons keep the sheet's per-reason
    /// specificity (lowercased to the design's mono voice).
    static func statusText(for state: DaemonInstallController.State) -> String {
        switch state {
        case .idle:
            return "recording runs through a background helper — approve it first"
        case .registering:
            return "starting helper…"
        case .requiresApproval:
            return "approve Screencap in System Settings → Login Items"
        case .polling:
            return "waiting for the helper to start…"
        case .installedAndRunning:
            return "helper running"
        case .pollingFailed(let reason):
            return reason.lowercased()
        case .installFailed(let reason):
            return Self.failureCopy(reason)
        }
    }

    static func statusColor(for state: DaemonInstallController.State) -> Color {
        switch state {
        case .installFailed, .pollingFailed: return .scRust
        case .installedAndRunning: return .scTeal
        default: return .scAmberText
        }
    }

    static func failureCopy(_ reason: DaemonInstallController.InstallFailureReason) -> String {
        switch reason {
        case .plistWriteFailed:
            return "the helper plist was not found in the app bundle"
        case .launchctlBootstrapFailed:
            return "macos did not start the helper — retry after approving login items"
        case .daemonDidNotStart:
            return "the helper did not respond after launch"
        case .daemonSigningInvalid:
            return "macos rejected the helper signature"
        case .diskFull:
            return "the disk is full, so the helper could not be installed"
        case .daemonVersionMismatch:
            return "a different helper version is running — approve helper reinstalls the bundled one"
        case .unknown:
            return "the helper could not be installed — retry"
        }
    }
}
