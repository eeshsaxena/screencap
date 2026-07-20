import XCTest
@testable import Screencap

final class QuitProgressCountdownTests: XCTestCase {
    func testRunPublishesRemainingValuesWithoutRepeatingInitialValue() async {
        var updates: [Int] = []

        await QuitProgressCountdown.run(totalSeconds: 3) {
            updates.append($0)
        } sleep: {
            // No-op sleep for a deterministic countdown sequence.
        }

        XCTAssertEqual(updates, [2, 1, 0])
    }

    func testRunStopsImmediatelyWhenSleepThrows() async {
        var updates: [Int] = []

        await QuitProgressCountdown.run(totalSeconds: 3) {
            updates.append($0)
        } sleep: {
            throw CancellationError()
        }

        XCTAssertEqual(updates, [])
    }
}
