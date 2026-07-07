import CoreGraphics
import XCTest
@testable import ScreenCap

/// U3 — pure gating for the bottom-edge "Show controls" peek: the reveal AND-gate
/// (recording × hidden × cursor-in-band) and the Dock-clearing band geometry
/// (anchored on the visible frame, not the physical screen bottom).
final class HUDPeekPolicyTests: XCTestCase {
    func testShouldRevealRequiresRecordingHiddenAndInBand() {
        XCTAssertTrue(HUDPeekPolicy.shouldReveal(isRecording: true, hudHidden: true, cursorInBand: true))
        XCTAssertFalse(HUDPeekPolicy.shouldReveal(isRecording: false, hudHidden: true, cursorInBand: true))
        XCTAssertFalse(HUDPeekPolicy.shouldReveal(isRecording: true, hudHidden: false, cursorInBand: true))
        XCTAssertFalse(HUDPeekPolicy.shouldReveal(isRecording: true, hudHidden: true, cursorInBand: false))
    }

    /// The band is measured from `visibleFrame.minY`, so a cursor in the Dock zone
    /// *below* the visible frame is NOT in band — the peek clears a bottom Dock.
    func testCursorInBottomBandAnchorsOnVisibleFrame() {
        // Visible frame starts 100pt up from the physical bottom (a Dock lives below).
        let visible = CGRect(x: 0, y: 100, width: 1000, height: 800)

        // Just above the visible bottom → in band.
        XCTAssertTrue(HUDPeekPolicy.cursorInBottomBand(
            cursor: CGPoint(x: 500, y: 103), visibleFrame: visible, band: 6))
        // In the Dock zone below the visible frame → NOT in band (Dock-clearing).
        XCTAssertFalse(HUDPeekPolicy.cursorInBottomBand(
            cursor: CGPoint(x: 500, y: 50), visibleFrame: visible, band: 6))
        // Well above the band → out.
        XCTAssertFalse(HUDPeekPolicy.cursorInBottomBand(
            cursor: CGPoint(x: 500, y: 400), visibleFrame: visible, band: 6))
        // Outside the horizontal extent → out.
        XCTAssertFalse(HUDPeekPolicy.cursorInBottomBand(
            cursor: CGPoint(x: 1200, y: 103), visibleFrame: visible, band: 6))
    }
}
