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
}

/// Test-only stand-in. Returns a fixed reply for Cmd+Q and either invokes
/// or skips the openSettings closure for permission-lost.
@MainActor
final class FakeRecorderAlertPresenter: RecorderAlertPresenter {
    let stopAndQuitReply: NSApplication.TerminateReply
    let permissionLostOpensSettings: Bool
    private(set) var lastPermissionPresented: String?
    private(set) var lastPermissionRequiredPresented: [String]?

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
}
