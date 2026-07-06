import XCTest
@testable import ScreenCap

/// Hide control — the menu-bar "Show recording controls" visibility gate.
final class MenuBarMenuPolicyTests: XCTestCase {

    func testVisibleOnlyWhileRecordingAndHidden() {
        XCTAssertTrue(MenuBarMenuPolicy.showRecordingControlsVisible(
            state: .recording(elapsed: 12), hudHidden: true
        ))
    }

    func testHiddenWhenPillIsShown() {
        XCTAssertFalse(MenuBarMenuPolicy.showRecordingControlsVisible(
            state: .recording(elapsed: 12), hudHidden: false
        ))
    }

    /// No restorable pill exists outside `.recording`, even if the flag is set.
    func testHiddenOutsideRecording() {
        let states: [RecordingState] = [
            .idle, .starting, .stopping(quitting: false), .stopping(quitting: true),
        ]
        for state in states {
            XCTAssertFalse(
                MenuBarMenuPolicy.showRecordingControlsVisible(state: state, hudHidden: true),
                "no restorable pill in \(state)"
            )
        }
    }
}
