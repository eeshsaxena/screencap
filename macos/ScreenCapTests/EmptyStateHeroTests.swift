import XCTest
@testable import ScreenCap

/// The calendar detail's centered hero must flip from the first-run welcome to
/// an active-capture hero the moment recording starts — the fix for "from this
/// menu it's not clear that the recording started."
///
/// Before this branch existed the hero was gated only on `totalCount == 0`, and
/// during a user's *first* recording `RecordingsIndex.totalCount` stays 0 (the
/// index refreshes only on `recording_finalized`). So the "click Start" welcome
/// kept dominating the window for the whole recording, contradicting the live
/// capture while the only truthful signals (thin banner, menubar glyph,
/// overflowed toolbar Stop) were peripheral.
final class EmptyStateHeroTests: XCTestCase {

    func testIdleShowsWelcome() {
        XCTAssertEqual(EmptyStateHero.resolve(state: .idle), .welcome)
    }

    func testStartingShowsRecordingHero() {
        XCTAssertEqual(
            EmptyStateHero.resolve(state: .starting),
            .recording(headline: "Starting…")
        )
    }

    func testRecordingShowsRecordingHero() {
        // The core regression: an active recording must NOT present the welcome.
        XCTAssertEqual(
            EmptyStateHero.resolve(state: .recording(elapsed: 12)),
            .recording(headline: "Recording in progress")
        )
        XCTAssertNotEqual(EmptyStateHero.resolve(state: .recording(elapsed: 12)), .welcome)
    }

    func testStoppingShowsRecordingHero() {
        XCTAssertEqual(
            EmptyStateHero.resolve(state: .stopping(quitting: false)),
            .recording(headline: "Finishing up…")
        )
        XCTAssertEqual(
            EmptyStateHero.resolve(state: .stopping(quitting: true)),
            .recording(headline: "Finishing up…")
        )
    }
}
