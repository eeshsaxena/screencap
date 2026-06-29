import XCTest
@testable import ScreenCap

/// U2 tests — the Phase 1c migration marker store (SCR-49). Pure file-IO
/// contract, exercised against an injected temp base directory so the real
/// `~/.screencap/.tcc-migrated-v1` is never touched.
final class MigrationMarkerStoreTests: XCTestCase {
    private var tempBase: URL!

    override func setUpWithError() throws {
        try super.setUpWithError()
        // A unique temp dir per test that does NOT yet exist — markMigrated()
        // is responsible for creating it (mirrors the absent-`~/.screencap` case).
        tempBase = FileManager.default.temporaryDirectory
            .appendingPathComponent("screencap-marker-tests", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
    }

    override func tearDownWithError() throws {
        if FileManager.default.fileExists(atPath: tempBase.path) {
            try FileManager.default.removeItem(at: tempBase)
        }
        try super.tearDownWithError()
    }

    func testIsMigratedFalseOnCleanBase() {
        let store = MigrationMarkerStore(baseDirectory: tempBase)
        XCTAssertFalse(store.isMigrated(), "a never-written base must read as not migrated")
    }

    func testMarkMigratedThenIsMigratedTrue() throws {
        let store = MigrationMarkerStore(baseDirectory: tempBase)
        try store.markMigrated()
        XCTAssertTrue(store.isMigrated(), "marker must be observable after markMigrated()")
    }

    func testMarkMigratedCreatesAbsentBaseDirectory() throws {
        XCTAssertFalse(
            FileManager.default.fileExists(atPath: tempBase.path),
            "precondition: base dir absent"
        )
        let store = MigrationMarkerStore(baseDirectory: tempBase)
        try store.markMigrated()
        // The base dir now exists and holds exactly the versioned marker file.
        let markerPath = tempBase.appendingPathComponent(MigrationMarkerStore.markerName).path
        XCTAssertTrue(FileManager.default.fileExists(atPath: markerPath))
    }

    func testMarkMigratedIsIdempotent() throws {
        let store = MigrationMarkerStore(baseDirectory: tempBase)
        try store.markMigrated()
        // A second write over the existing marker must not throw or clear it.
        XCTAssertNoThrow(try store.markMigrated())
        XCTAssertTrue(store.isMigrated())
    }

    func testMarkMigratedThrowsWhenBaseCannotBeCreated() throws {
        // Make the parent a regular *file*, so creating a directory at the child
        // path is impossible — markMigrated() must surface the IO error rather
        // than crash (the caller logs it; the banner re-shows once, harmlessly).
        let parent = FileManager.default.temporaryDirectory
            .appendingPathComponent("screencap-marker-blocker-\(UUID().uuidString)")
        try Data().write(to: parent, options: .atomic)
        defer { try? FileManager.default.removeItem(at: parent) }

        let blockedBase = parent.appendingPathComponent("nested", isDirectory: true)
        let store = MigrationMarkerStore(baseDirectory: blockedBase)
        XCTAssertThrowsError(try store.markMigrated())
        XCTAssertFalse(store.isMigrated())
    }
}
