import AppKit
import XCTest
@testable import ScreenCap

/// Lightweight coverage of the alert presenter protocol — the live
/// implementation calls `NSAlert.runModal()` synchronously so we can't
/// exercise it directly in an XCTest run loop. Most coverage of the
/// quit/permission flows lives in `RecorderControllerTests` via a fake
/// presenter that returns a fixed `TerminateReply`.
@MainActor
final class RecorderAlertPresenterTests: XCTestCase {
    func testFakePresenterDrivesConfirmStopAndQuit() {
        let presenter = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateLater)

        XCTAssertEqual(presenter.confirmStopAndQuit(), .terminateLater)
    }

    func testFakePresenterInvokesOpenSettingsClosure() {
        let presenter = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel, permissionLostOpensSettings: true)
        var opened = false

        presenter.presentPermissionLost(permission: "Screen Recording") { opened = true }

        XCTAssertTrue(opened)
        XCTAssertEqual(presenter.lastPermissionPresented, "Screen Recording")
    }

    func testFakePresenterSkipsOpenSettingsWhenDismissed() {
        let presenter = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel, permissionLostOpensSettings: false)
        var opened = false

        presenter.presentPermissionLost(permission: "Screen Recording") { opened = true }

        XCTAssertFalse(opened)
        XCTAssertEqual(presenter.lastPermissionPresented, "Screen Recording")
    }

    /// Pins the permission-lost copy (SCR-87). The same modal fires for a true
    /// revocation and for a fresh dev-build identity TCC has never granted, so
    /// the wording must read sensibly for both and must NOT assert the user
    /// disabled anything. Exact-string equality guards against a silent copy
    /// edit; the `disabled` check encodes the specific SCR-87 invariant so even
    /// a future reword can't reintroduce the misleading framing.
    func testPermissionLostCopyIsNeutralOnCause() {
        // Pass the user-facing display name — the form `handlePermissionLost`
        // now resolves the raw daemon token to before presenting (SCR-87).
        let body = LiveRecorderAlertPresenter.permissionLostBody(permission: "Screen Recording")

        XCTAssertEqual(LiveRecorderAlertPresenter.permissionLostTitle, "Recording stopped")
        XCTAssertEqual(
            body,
            "ScreenCap doesn't have permission to Screen Recording. "
                + "Enable it in System Settings, then start a new recording."
        )
        // The "disabled"/"revoked" invariants apply to BOTH title and body so a
        // future edit can't reintroduce the misleading framing in either string.
        XCTAssertFalse(
            body.lowercased().contains("disabled"),
            "Permission-lost copy must not assert the user disabled anything (SCR-87)."
        )
        XCTAssertFalse(
            LiveRecorderAlertPresenter.permissionLostTitle.lowercased().contains("disabled"),
            "Permission-lost title must not assert the user disabled anything (SCR-87)."
        )
        XCTAssertFalse(
            body.lowercased().contains("revoked"),
            "Permission-lost copy must not assert a revocation that did not happen (SCR-87)."
        )
        XCTAssertFalse(
            LiveRecorderAlertPresenter.permissionLostTitle.lowercased().contains("revoked"),
            "Permission-lost title must not assert a revocation that did not happen (SCR-87)."
        )
    }

    /// SCR-254 U9: the mic-access-denied modal must stay honest that the recording
    /// keeps running (only the mic couldn't turn on) — it must NOT say a recording
    /// "stopped", which would be false (R3).
    func testMicrophoneAccessDeniedCopyKeepsRecordingRunning() {
        let title = LiveRecorderAlertPresenter.microphoneAccessDeniedTitle
        let body = LiveRecorderAlertPresenter.microphoneAccessDeniedBody()

        XCTAssertEqual(title, "Microphone access needed")
        XCTAssertTrue(body.contains("keeps running"), "must say the recording continues")
        XCTAssertFalse(
            body.lowercased().contains("stopped"),
            "a denied unmute never stops the recording (R3)."
        )
    }

    func testFakePresenterRecordsMicrophoneAccessDenied() {
        let presenter = FakeRecorderAlertPresenter(
            stopAndQuitReply: .terminateCancel, permissionLostOpensSettings: true
        )
        var opened = false

        presenter.presentMicrophoneAccessDenied { opened = true }

        XCTAssertEqual(presenter.microphoneAccessDeniedPresentedCount, 1)
        XCTAssertTrue(opened)
    }
}

/// Test-only stand-in. Returns a fixed reply for Cmd+Q and either invokes
/// or skips the openSettings closure for permission-lost.
@MainActor
final class FakeRecorderAlertPresenter: RecorderAlertPresenter {
    let stopAndQuitReply: NSApplication.TerminateReply
    let permissionLostOpensSettings: Bool
    private(set) var lastPermissionPresented: String?
    private(set) var lastPermissionRequiredPresented: [String]?
    /// SCR-254 U9: number of times the microphone-access-denied modal was
    /// presented, so a test can assert the unmute-denied path routed to the modal
    /// (not inline-only) for the menu-bar / HUD-hidden case.
    private(set) var microphoneAccessDeniedPresentedCount = 0

    init(stopAndQuitReply: NSApplication.TerminateReply, permissionLostOpensSettings: Bool = false) {
        self.stopAndQuitReply = stopAndQuitReply
        self.permissionLostOpensSettings = permissionLostOpensSettings
    }

    func confirmStopAndQuit() -> NSApplication.TerminateReply {
        stopAndQuitReply
    }

    func presentPermissionLost(permission: String, openSettings: @MainActor () -> Void) {
        lastPermissionPresented = permission
        if permissionLostOpensSettings {
            openSettings()
        }
    }

    func presentPermissionRequired(permissions: [String], openSettings: @MainActor () -> Void) {
        lastPermissionRequiredPresented = permissions
        if permissionLostOpensSettings {
            openSettings()
        }
    }

    func presentMicrophoneAccessDenied(openSettings: @MainActor () -> Void) {
        microphoneAccessDeniedPresentedCount += 1
        if permissionLostOpensSettings {
            openSettings()
        }
    }
}
