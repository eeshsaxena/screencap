import AppKit
import XCTest
@testable import Screencap

/// Capture-exclusion invariant (R9 / KTD-7): every HUD floating surface built via
/// the shared factory is excluded from the recording (`sharingType = .none`) and
/// non-activating, so it never appears in the capture nor steals focus from the
/// recorded app. Guards the P0 "a leak is a blocker" rule against a future panel
/// that forgets the exclusion.
@MainActor
final class HUDPanelTests: XCTestCase {
    func testCaptureExcludedPanelNeverEntersRecording() {
        let panel = HUDPanel.captureExcluded(
            contentRect: NSRect(x: 0, y: 0, width: 120, height: 44),
            contentView: NSView()
        )

        XCTAssertEqual(panel.sharingType, .none, "HUD surfaces must never appear in the recording (R9)")
        XCTAssertTrue(panel.styleMask.contains(.nonactivatingPanel), "must not steal focus from the recorded app")
        XCTAssertFalse(panel.hasShadow, "the SwiftUI content owns the lift, not the panel")
        XCTAssertTrue(panel.collectionBehavior.contains(.canJoinAllSpaces), "rides across Spaces / full-screen apps")
    }
}
