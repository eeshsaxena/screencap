import XCTest
@testable import ScreenCap

/// U7 — the recording HUD's pure content model: the mono elapsed format, the
/// VoiceOver labels, and the honesty-substituted footer (KTD-9).
final class RecordingHUDModelTests: XCTestCase {

    private func model(elapsed: TimeInterval, title: String = "demo", audio: Bool = true) -> RecordingHUDModel {
        RecordingHUDModel(elapsed: elapsed, title: title, audioEnabled: audio)
    }

    // MARK: - elapsedText (design's mono MM:SS)

    func testElapsedFormatsAsMinutesSecondsUnderAnHour() {
        XCTAssertEqual(model(elapsed: 0).elapsedText, "00:00")
        XCTAssertEqual(model(elapsed: 9).elapsedText, "00:09")
        XCTAssertEqual(model(elapsed: 272).elapsedText, "04:32")   // the design's 04:32
        XCTAssertEqual(model(elapsed: 59.9).elapsedText, "00:59")  // truncates, never rounds up
    }

    func testElapsedGrowsToHoursPastAnHour() {
        XCTAssertEqual(model(elapsed: 3600).elapsedText, "1:00:00")
        XCTAssertEqual(model(elapsed: 3661).elapsedText, "1:01:01")
    }

    func testElapsedClampsNegative() {
        XCTAssertEqual(model(elapsed: -5).elapsedText, "00:00")
    }

    // MARK: - Accessibility labels

    func testElapsedAccessibilityIsSpoken() {
        XCTAssertEqual(model(elapsed: 272).elapsedAccessibilityLabel, "Recording time 4 minutes 32 seconds")
        XCTAssertEqual(model(elapsed: 1).elapsedAccessibilityLabel, "Recording time 1 second")
        XCTAssertEqual(model(elapsed: 3661).elapsedAccessibilityLabel, "Recording time 1 hour 1 minute 1 second")
    }

    func testTitleAndStopAccessibilityLabels() {
        let m = model(elapsed: 10, title: "Payroll walkthrough")
        XCTAssertEqual(m.titleAccessibilityLabel, "Recording Payroll walkthrough")
        XCTAssertEqual(m.stopAccessibilityLabel, "Stop and save recording")
    }

    func testHideAccessibilityLabel() {
        XCTAssertEqual(model(elapsed: 10).hideAccessibilityLabel, "Hide recording controls")
    }

    // MARK: - Footer honesty (KTD-9)

    func testFooterNeverClaimsEncryption() {
        XCTAssertEqual(RecordingHUDModel.footerText, "recording to this Mac")
        XCTAssertFalse(RecordingHUDModel.footerText.lowercased().contains("encrypted"))
    }
}
