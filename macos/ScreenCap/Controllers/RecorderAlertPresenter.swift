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

    /// Shows the "Permission revoked" alert. Invokes `openSettings` when the
    /// user clicks **Open System Settings**.
    func presentPermissionLost(permission: String, openSettings: @MainActor () -> Void)
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

    func presentPermissionLost(permission: String, openSettings: @MainActor () -> Void) {
        let alert = NSAlert()
        alert.messageText = "Permission revoked"
        alert.informativeText = "ScreenCap stopped recording because \(permission) was disabled in System Settings."
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn {
            openSettings()
        }
    }
}
