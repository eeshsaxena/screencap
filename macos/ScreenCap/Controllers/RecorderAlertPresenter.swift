import AppKit
import Foundation

/// AppKit modal flows the recorder needs to surface — extracted so the
/// orchestrator stays free of `NSAlert` and so tests can substitute a fake
/// that returns a fixed `TerminateReply` without spinning a modal.
@MainActor
protocol RecorderAlertPresenter {
    /// Shows the Cmd+Q "Stop recording before quitting?" sheet. Returns
    /// `.terminateLater` if the user chose **Stop & Quit** and
    /// `.terminateCancel` for **Keep Recording** or **Cancel**. The
    /// orchestrator maps this into the stop policy + countdown.
    func confirmStopAndQuit() -> NSApplication.TerminateReply

    /// Shows the "Recording stopped" permission-lost alert. The copy is
    /// intentionally neutral on cause (SCR-87): the same modal fires for a true
    /// mid-recording TCC revocation and for a fresh dev-build whose code-signing
    /// identity TCC has never granted. Invokes `openSettings` when the user
    /// clicks **Open System Settings**.
    func presentPermissionLost(permission: String, openSettings: @MainActor () -> Void)

    /// Shows the "permission required *before* recording" alert (U4/U6). Distinct
    /// from `presentPermissionLost` because nothing was recording — the copy must
    /// not say a recording "stopped". Invokes `openSettings` on **Open System
    /// Settings**. `permissions` lists the missing permission display names.
    func presentPermissionRequired(permissions: [String], openSettings: @MainActor () -> Void)

    /// Shows the "microphone access denied" alert for a failed UNMUTE (SCR-254 U9).
    /// Distinct from both alerts above: the recording keeps running (nothing
    /// "stopped", nothing is blocked from "starting") — only the mic could not be
    /// turned on, so it stays muted. The one surface guaranteed visible during a
    /// recording (the HUD pill shows no inline error, the main window is hidden), so
    /// the menu-bar / HUD-hidden denial routes here (R3, never silent). Invokes
    /// `openSettings` on **Open System Settings**.
    func presentMicrophoneAccessDenied(openSettings: @MainActor () -> Void)
}

/// Live implementation. Produces the same wording and button order as the
/// pre-extraction inline alerts to preserve user-facing behavior verbatim.
@MainActor
final class LiveRecorderAlertPresenter: RecorderAlertPresenter {
    func confirmStopAndQuit() -> NSApplication.TerminateReply {
        let alert = NSAlert()
        alert.messageText = "Stop recording before quitting?"
        alert.informativeText =
            "ScreenCap is still recording. Stop & Quit saves the recording — finalization can take up to 5 minutes."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Stop & Quit")
        alert.addButton(withTitle: "Keep Recording")
        alert.addButton(withTitle: "Cancel")

        switch alert.runModal() {
        case .alertFirstButtonReturn:
            return .terminateLater
        default:
            return .terminateCancel
        }
    }

    /// Title for the permission-lost modal. Extracted as a pure static so a
    /// test can pin the wording without driving `NSAlert.runModal()` (SCR-87).
    ///
    /// Stays neutral on cause (see `permissionLostBody`): "Recording stopped" is
    /// the one fact true in every case — `handlePermissionLost` always calls
    /// `stop()`. Deliberately NOT "Permission revoked" (asserts a revocation
    /// that never happened for fresh-build identity denials) nor "Permission
    /// required" (the distinct pre-recording modal `presentPermissionRequired`
    /// owns that title).
    static let permissionLostTitle = "Recording stopped"

    /// Body for the permission-lost modal.
    ///
    /// Must stay **neutral on cause**: the same modal fires both for a true
    /// mid-recording revocation (user toggled the permission off) and for a
    /// fresh dev-build whose new code-signing identity TCC has never granted —
    /// the `permission_lost` event carries no field distinguishing them. So the
    /// copy must NOT assert the user disabled anything in System Settings, or it
    /// sends the identity-denial case hunting for a toggle they never touched
    /// (SCR-87).
    static func permissionLostBody(permission: String) -> String {
        "ScreenCap doesn't have permission to \(permission). "
            + "Enable it in System Settings, then start a new recording."
    }

    func presentPermissionLost(permission: String, openSettings: @MainActor () -> Void) {
        let alert = NSAlert()
        alert.messageText = Self.permissionLostTitle
        alert.informativeText = Self.permissionLostBody(permission: permission)
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn {
            openSettings()
        }
    }

    func presentPermissionRequired(permissions: [String], openSettings: @MainActor () -> Void) {
        let names = permissions.isEmpty ? "a required permission" : permissions.joined(separator: ", ")
        let alert = NSAlert()
        alert.messageText = "Permission required"
        alert.informativeText =
            "ScreenCap can't start recording until \(names) is granted in System Settings."
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn {
            openSettings()
        }
    }

    /// Title for the microphone-access-denied modal (SCR-254 U9). Extracted as a
    /// pure static so a test can pin the wording without driving `runModal()`.
    static let microphoneAccessDeniedTitle = "Microphone access needed"

    /// Body for the microphone-access-denied modal. Must stay honest that the
    /// recording is still running (only muted) — a denied unmute never stops the
    /// recording (R3), so the copy must not imply it did.
    static func microphoneAccessDeniedBody() -> String {
        "ScreenCap can't turn the microphone on because microphone access is denied. "
            + "Enable it in System Settings; the recording keeps running, muted."
    }

    func presentMicrophoneAccessDenied(openSettings: @MainActor () -> Void) {
        let alert = NSAlert()
        alert.messageText = Self.microphoneAccessDeniedTitle
        alert.informativeText = Self.microphoneAccessDeniedBody()
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn {
            openSettings()
        }
    }
}
