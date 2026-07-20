import XCTest
@testable import Screencap

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

    /// SCR-239 (U11) — the local-model hint dismissal flag round-trips and
    /// persists (once dismissed, never re-prompt — R7).
    func testLocalModelHintDismissalPersists() {
        let suite = "hud-hint-store-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let store = HUDHintStore(defaults: defaults)
        XCTAssertFalse(store.hasDismissedLocalModelHint, "defaults to not-dismissed")

        store.markLocalModelHintDismissed()
        XCTAssertTrue(store.hasDismissedLocalModelHint)
        XCTAssertTrue(HUDHintStore(defaults: defaults).hasDismissedLocalModelHint,
                      "survives relaunch")
        // Independent of the hide-hint flag.
        XCTAssertFalse(store.hasShownHideHint)
    }
}
