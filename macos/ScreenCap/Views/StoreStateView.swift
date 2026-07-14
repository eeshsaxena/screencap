import SwiftUI

/// Pure, unit-testable copy for the non-mounted store states (SCR-258 U10, R10,
/// AE8). Keeping the strings + affordance flags in one value type means the
/// Library, Chat, and Search surfaces render identical guidance, and the R10
/// wording is asserted in one place (`StoreStateCopyTests`) rather than three.
struct StoreStateCopy: Equatable {
    let systemImage: String
    let title: String
    let message: String
    /// Only `.locked` shows Unlock — the sealed state a present-user (Touch ID)
    /// unlock can open.
    let showsUnlock: Bool
    /// A retry re-checks the store; offered only for retryable errors (a locked
    /// Keychain, an unknown cause). Key-missing / entitlement / downgrade never
    /// show it — retrying can't help (R10).
    let showsRetry: Bool
    /// `.absent` shows a "Set up encrypted storage" action that runs `storage init`.
    let showsSetup: Bool
    /// Drives the icon tint (a red/amber alarm vs the calm neutral of a routine
    /// locked/absent state).
    let isAlarm: Bool

    /// The copy for a state, or `nil` for `.mounted` (nothing to render).
    static func copy(for state: StoreState) -> StoreStateCopy? {
        switch state {
        case .mounted:
            return nil
        case .locked:
            return StoreStateCopy(
                systemImage: "lock.fill",
                title: "Library is locked",
                message: "Your recordings are sealed and encrypted on this Mac. "
                    + "Unlock with Touch ID to view them.",
                showsUnlock: true, showsRetry: false, showsSetup: false, isAlarm: false
            )
        case .absent:
            return StoreStateCopy(
                systemImage: "externaldrive.badge.plus",
                title: "Set up encrypted storage",
                message: "Turn on encrypted storage to keep every recording as "
                    + "ciphertext at rest on this Mac.",
                showsUnlock: false, showsRetry: false, showsSetup: true, isAlarm: false
            )
        case .error(let reason):
            return errorCopy(reason)
        }
    }

    private static func errorCopy(_ reason: StoreErrorReason) -> StoreStateCopy {
        switch reason {
        case .keyMissing:
            // R10 / AE5 (app arm): the unrecoverable case — NO Unlock, NO Retry.
            return StoreStateCopy(
                systemImage: "exclamationmark.triangle.fill",
                title: "These recordings can't be recovered",
                message: "The encryption key is missing, so these recordings can't "
                    + "be opened. Nothing on disk was deleted.",
                showsUnlock: false, showsRetry: false, showsSetup: false, isAlarm: true
            )
        case .entitlementMismatch:
            return StoreStateCopy(
                systemImage: "lock.trianglebadge.exclamationmark",
                title: "This app can't reach the encryption key",
                message: "The key exists, but this build isn't allowed to read it. "
                    + "Open the ScreenCap app (or the bundled screencap CLI) to "
                    + "access these recordings.",
                showsUnlock: false, showsRetry: false, showsSetup: false, isAlarm: true
            )
        case .keychainLocked:
            return StoreStateCopy(
                systemImage: "lock.rotation",
                title: "Your login Keychain is locked",
                message: "Unlock your Mac's login Keychain, then try again — your "
                    + "recordings stay safe.",
                showsUnlock: false, showsRetry: true, showsSetup: false, isAlarm: false
            )
        case .downgradeUnsupported:
            return StoreStateCopy(
                systemImage: "lock.shield",
                title: "Encrypted storage can't be turned off",
                message: "This install already keeps recordings encrypted at rest, "
                    + "so it can't fall back to plaintext. Re-enable encrypted "
                    + "storage to access your recordings.",
                showsUnlock: false, showsRetry: false, showsSetup: false, isAlarm: true
            )
        case .unknown:
            return StoreStateCopy(
                systemImage: "exclamationmark.triangle",
                title: "The encrypted store couldn't be opened",
                message: "Something prevented the encrypted store from opening. "
                    + "Try again in a moment.",
                showsUnlock: false, showsRetry: true, showsSetup: false, isAlarm: true
            )
        }
    }
}

/// First-class rendering of a non-mounted encrypted store, shared by the Library,
/// Chat, and Search surfaces (SCR-258 U10, KTD-20, AE8). It replaces the empty /
/// errored surface so a locked / absent / key-missing store never reads as data
/// loss (R6). The Unlock button runs the store-scoped `PresenceGate` (Touch ID)
/// then the unlock verb, via the injected `onUnlock` closure — this view owns no
/// LocalAuthentication itself (no eager LA on render, KTD-16).
struct StoreStateView: View {
    let storeState: StoreState
    /// Present-user-gated unlock (`.locked` only). nil hides the button.
    var onUnlock: (() -> Void)?
    /// Re-check the store (retryable errors). nil hides the button.
    var onRetry: (() -> Void)?
    /// Turn on encrypted storage (`.absent`). nil hides the button.
    var onSetup: (() -> Void)?
    /// A lock/unlock/setup round-trip is in flight — disables the action + relabels.
    var isBusy: Bool = false
    /// Tighter spacing for the Recall palette panel (vs the full-area Library/Chat).
    var compact: Bool = false

    var body: some View {
        if let copy = StoreStateCopy.copy(for: storeState) {
            VStack(spacing: compact ? SCMetrics.space2 : SCMetrics.space4) {
                Image(systemName: copy.systemImage)
                    .font(.system(size: compact ? 24 : 34))
                    .foregroundStyle(copy.isAlarm ? Color.scErrorFg : Color.scInkSecondary)
                    .accessibilityHidden(true)
                Text(copy.title)
                    .font(compact ? SCTypography.sans(size: 13.5, weight: .semibold) : SCTypography.sectionTitle)
                    .foregroundStyle(Color.scInk)
                Text(copy.message)
                    .font(compact ? SCTypography.sans(size: 12) : SCTypography.bodyText)
                    .foregroundStyle(Color.scInkSecondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: compact ? 360 : 420)
                actions(copy)
            }
            .frame(maxWidth: .infinity, maxHeight: compact ? nil : .infinity)
            .padding(compact ? SCMetrics.space6 : SCMetrics.space8)
            .accessibilityElement(children: .combine)
        }
    }

    @ViewBuilder
    private func actions(_ copy: StoreStateCopy) -> some View {
        HStack(spacing: SCMetrics.space2) {
            if copy.showsUnlock, let onUnlock {
                Button(isBusy ? "Unlocking…" : "Unlock") { onUnlock() }
                    .buttonStyle(.borderedProminent)
                    .disabled(isBusy)
                    .accessibilityHint("Requires Touch ID or your login password.")
            }
            if copy.showsSetup, let onSetup {
                Button(isBusy ? "Setting up…" : "Set up encrypted storage") { onSetup() }
                    .buttonStyle(.borderedProminent)
                    .disabled(isBusy)
            }
            if copy.showsRetry, let onRetry {
                Button("Try again") { onRetry() }
                    .disabled(isBusy)
            }
        }
        .padding(.top, compact ? 2 : SCMetrics.space1)
    }
}
