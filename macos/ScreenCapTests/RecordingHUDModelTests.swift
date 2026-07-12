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

    // MARK: - Mute control (SCR-254 U8)

    private func micModel(
        muted: Bool, audio: Bool = true, inFlight: Bool = false
    ) -> RecordingHUDModel {
        RecordingHUDModel(
            elapsed: 10, title: "demo", audioEnabled: audio, muted: muted, muteInFlight: inFlight
        )
    }

    /// Audio-on and unmuted: the mic reads "Mic on" with the plain mic glyph.
    func testMicControlUnmutedShowsMicOn() {
        let m = micModel(muted: false)
        XCTAssertFalse(m.micEffectivelyMuted)
        XCTAssertEqual(m.micStatusLabel, "Mic on")
        XCTAssertEqual(m.micIconName, "mic.fill")
        XCTAssertEqual(m.micAccessibilityLabel, "Microphone on, tap to mute")
    }

    /// Muted: the mic reads "Muted" with the slashed glyph, and the a11y label
    /// spells out the unmute action so "Muted" is never read as a bare command.
    func testMicControlMutedShowsMutedWithSlashIcon() {
        let m = micModel(muted: true)
        XCTAssertTrue(m.micEffectivelyMuted)
        XCTAssertEqual(m.micStatusLabel, "Muted")
        XCTAssertEqual(m.micIconName, "mic.slash.fill")
        XCTAssertEqual(m.micAccessibilityLabel, "Microphone muted, tap to unmute")
    }

    /// A recording that started audio-off (never unmuted) reads as "Muted" — its
    /// mic is not capturing — so the control offers to turn it on (R2).
    func testMicControlAudioOffReadsAsMuted() {
        let m = micModel(muted: false, audio: false)
        XCTAssertTrue(m.micEffectivelyMuted)
        XCTAssertEqual(m.micStatusLabel, "Muted")
        XCTAssertEqual(m.micIconName, "mic.slash.fill")
    }

    /// In-flight shows the DIRECTION from the current confirmed state (never an
    /// optimistic target): muting from on, unmuting from muted (KTD4).
    func testMicControlInFlightShowsTransitionalLabel() {
        XCTAssertEqual(micModel(muted: false, inFlight: true).micStatusLabel, "Muting…")
        XCTAssertEqual(micModel(muted: false, inFlight: true).micAccessibilityLabel, "Muting microphone")
        XCTAssertEqual(micModel(muted: true, inFlight: true).micStatusLabel, "Unmuting…")
        XCTAssertEqual(micModel(muted: true, inFlight: true).micAccessibilityLabel, "Unmuting microphone")
    }
}
