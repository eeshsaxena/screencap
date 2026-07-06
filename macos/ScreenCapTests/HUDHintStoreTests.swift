import XCTest
@testable import ScreenCap

/// U5 — the one-time-hint flag round-trips through an isolated `UserDefaults`
/// suite and persists across store instances (never touches `.standard`).
final class HUDHintStoreTests: XCTestCase {
    func testStoreRoundTripAndPersistence() {
        let suite = "hud-hint-store-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let store = HUDHintStore(defaults: defaults)
        XCTAssertFalse(store.hasShownHideHint, "defaults to not-shown")

        store.markShown()
        XCTAssertTrue(store.hasShownHideHint)

        // A fresh store over the same suite still reads true (survives relaunch).
        let reopened = HUDHintStore(defaults: defaults)
        XCTAssertTrue(reopened.hasShownHideHint)
    }
}
