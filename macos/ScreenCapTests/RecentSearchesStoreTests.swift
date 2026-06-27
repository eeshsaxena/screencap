import XCTest
@testable import ScreenCap

/// SCR-182 U4 — drives `RecentSearchesStore` against an isolated UserDefaults
/// suite (mirrors `PermissionControllerTests`' injected-defaults pattern) so it
/// never touches `.standard`.
final class RecentSearchesStoreTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "test.recent.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        super.tearDown()
    }

    func testRecordsMostRecentFirst() {
        let store = RecentSearchesStore(defaults: defaults)
        store.record("refund")
        store.record("salesforce")
        XCTAssertEqual(store.recent, ["salesforce", "refund"])
    }

    func testRecordExistingMovesToFrontWithoutDuplicating() {
        let store = RecentSearchesStore(defaults: defaults)
        store.record("a")
        store.record("b")
        store.record("a")
        XCTAssertEqual(store.recent, ["a", "b"])
    }

    func testCapEvictsOldest() {
        let store = RecentSearchesStore(defaults: defaults)
        for i in 0..<(RecentSearchesStore.cap + 3) { store.record("q\(i)") }
        XCTAssertEqual(store.recent.count, RecentSearchesStore.cap)
        XCTAssertEqual(store.recent.first, "q\(RecentSearchesStore.cap + 2)", "newest leads")
        XCTAssertFalse(store.recent.contains("q0"), "oldest evicted")
    }

    func testEmptyOrWhitespaceNotRecorded() {
        let store = RecentSearchesStore(defaults: defaults)
        store.record("   ")
        store.record("")
        XCTAssertTrue(store.recent.isEmpty)
    }

    func testPersistsAcrossInstances() {
        RecentSearchesStore(defaults: defaults).record("persisted")
        let reloaded = RecentSearchesStore(defaults: defaults)
        XCTAssertEqual(reloaded.recent, ["persisted"], "recents survive a fresh instance (relaunch)")
    }

    func testCaseInsensitiveDedupeKeepsNewCasing() {
        let store = RecentSearchesStore(defaults: defaults)
        store.record("Refund")
        store.record("refund")
        XCTAssertEqual(store.recent, ["refund"])
    }
}
