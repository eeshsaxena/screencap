import XCTest
@testable import ScreenCap

/// SCR-258 U10: the pure copy + visibility policies for the encrypted-store
/// surfaces. Asserting the R10 wording rules (no Unlock on an unrecoverable store,
/// the auto-resuming paused copy) in one place per the test philosophy.
final class StorePolicyTests: XCTestCase {

    // MARK: - StoreStateCopy (R10 / AE8)

    func testMountedHasNoCopy() {
        XCTAssertNil(StoreStateCopy.copy(for: .mounted))
    }

    func testLockedOffersUnlockOnly() {
        let copy = try! XCTUnwrap(StoreStateCopy.copy(for: .locked))
        XCTAssertTrue(copy.showsUnlock)
        XCTAssertFalse(copy.showsRetry)
        XCTAssertFalse(copy.showsSetup)
        XCTAssertFalse(copy.isAlarm, "a locked store is a calm state, not an alarm")
    }

    func testAbsentOffersSetupOnly() {
        let copy = try! XCTUnwrap(StoreStateCopy.copy(for: .absent))
        XCTAssertTrue(copy.showsSetup)
        XCTAssertFalse(copy.showsUnlock)
        XCTAssertFalse(copy.showsRetry)
    }

    func testKeyMissingHasNoUnlockAndNoRetry() {
        // AE5 app arm / R10: the unrecoverable case — retrying can't help.
        let copy = try! XCTUnwrap(StoreStateCopy.copy(for: .error(reason: .keyMissing)))
        XCTAssertFalse(copy.showsUnlock)
        XCTAssertFalse(copy.showsRetry)
        XCTAssertTrue(copy.isAlarm)
        XCTAssertTrue(copy.title.lowercased().contains("can't be recovered"))
    }

    func testEntitlementMismatchNamesTheCauseNoUnlock() {
        let copy = try! XCTUnwrap(StoreStateCopy.copy(for: .error(reason: .entitlementMismatch)))
        XCTAssertFalse(copy.showsUnlock)
        XCTAssertFalse(copy.showsRetry)
    }

    func testKeychainLockedIsRetryable() {
        let copy = try! XCTUnwrap(StoreStateCopy.copy(for: .error(reason: .keychainLocked)))
        XCTAssertTrue(copy.showsRetry)
        XCTAssertFalse(copy.showsUnlock)
        XCTAssertTrue(StoreErrorReason.keychainLocked.isRetryable)
        XCTAssertFalse(StoreErrorReason.keyMissing.isRetryable)
    }

    // MARK: - MenuBarMenuPolicy Lock / Unlock visibility

    func testLockVisibleOnlyForMountedEnabledIdle() {
        XCTAssertTrue(MenuBarMenuPolicy.lockItemVisible(
            storeState: .mounted, containerEnabled: true, phase: .idle))
        // plaintext-only build (container disabled) → never offer Lock
        XCTAssertFalse(MenuBarMenuPolicy.lockItemVisible(
            storeState: .mounted, containerEnabled: false, phase: .idle))
        // sealed / absent → not a lock target
        XCTAssertFalse(MenuBarMenuPolicy.lockItemVisible(
            storeState: .locked, containerEnabled: true, phase: .idle))
        XCTAssertFalse(MenuBarMenuPolicy.lockItemVisible(
            storeState: .absent, containerEnabled: true, phase: .idle))
        // in-flight → hidden while "Locking…" shows
        XCTAssertFalse(MenuBarMenuPolicy.lockItemVisible(
            storeState: .mounted, containerEnabled: true, phase: .locking))
    }

    func testUnlockVisibleOnlyForSealedIdle() {
        XCTAssertTrue(MenuBarMenuPolicy.unlockItemVisible(storeState: .locked, phase: .idle))
        XCTAssertFalse(MenuBarMenuPolicy.unlockItemVisible(storeState: .mounted, phase: .idle))
        XCTAssertFalse(MenuBarMenuPolicy.unlockItemVisible(storeState: .absent, phase: .idle))
        XCTAssertFalse(MenuBarMenuPolicy.unlockItemVisible(storeState: .locked, phase: .unlocking))
    }

    // MARK: - VaultMigrationPolicy banner gating + paused copy

    func testBannerHiddenWhenContainerDisabledOrAlreadyEncrypted() {
        XCTAssertFalse(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: false, storeEncrypted: false, recordingsPresent: true,
            state: .idle, dismissed: false))
        XCTAssertFalse(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: true, recordingsPresent: true,
            state: .idle, dismissed: false))
    }

    func testBannerOfferGatedOnRecordingsAndDismiss() {
        XCTAssertTrue(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: false, recordingsPresent: true,
            state: .idle, dismissed: false))
        XCTAssertFalse(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: false, recordingsPresent: true,
            state: .idle, dismissed: true))
        XCTAssertFalse(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: false, recordingsPresent: false,
            state: .idle, dismissed: false))
    }

    func testBannerAlwaysShowsActiveOrPausedEvenAfterDismiss() {
        XCTAssertTrue(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: false, recordingsPresent: false,
            state: .migrating(.init(verified: 1, total: 3, phase: "copying")), dismissed: true))
        XCTAssertTrue(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: false, recordingsPresent: false,
            state: .paused(reason: "insufficient_disk"), dismissed: true))
    }

    func testCompletedBannerHidden() {
        XCTAssertFalse(VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: true, storeEncrypted: false, recordingsPresent: true,
            state: .succeeded, dismissed: false))
    }

    func testPausedRendersAutoResumeNotFailure() {
        let disk = VaultMigrationPolicy.statusLine(for: .paused(reason: "insufficient_disk"))
        XCTAssertEqual(disk, "Waiting for free disk space — will resume automatically.")
        // Any paused reason reads as auto-resuming, never a failure.
        let other = VaultMigrationPolicy.statusLine(for: .paused(reason: "something"))
        XCTAssertTrue(other?.contains("resume automatically") == true)
    }

    func testMigratingAndFailedStatusLines() {
        let migrating = VaultMigrationPolicy.statusLine(
            for: .migrating(.init(verified: 2, total: 5, phase: "copying")))
        XCTAssertTrue(migrating?.contains("2 of 5") == true)
        XCTAssertEqual(VaultMigrationPolicy.statusLine(for: .failed(message: "nope")), "nope")
        XCTAssertNil(VaultMigrationPolicy.statusLine(for: .idle))
    }

    // MARK: - EncryptMigrationState snapshot mapping

    func testEncryptStateMapping() {
        func snap(_ state: String, paused: String? = nil, verified: Int = 0, total: Int = 0)
            -> StorageEncryptResponse {
            let reasonJSON = paused.map { ", \"paused_reason\": \"\($0)\"" } ?? ""
            let json = """
            {"ok": true, "schema_version": 1, "daemon_version": "0.11.0",
             "api_schema_version": 1, "state": "\(state)", "phase": "copying",
             "pending": 0, "copied": 0, "verified": \(verified), "deleted": 0,
             "total": \(total)\(reasonJSON)}
            """
            return try! JSONDecoder().decode(StorageEncryptResponse.self, from: Data(json.utf8))
        }
        XCTAssertEqual(
            PrivacyController.EncryptMigrationState.from(snap("running", verified: 1, total: 4)),
            .migrating(.init(verified: 1, total: 4, phase: "copying")))
        XCTAssertEqual(
            PrivacyController.EncryptMigrationState.from(snap("paused", paused: "insufficient_disk")),
            .paused(reason: "insufficient_disk"))
        XCTAssertEqual(
            PrivacyController.EncryptMigrationState.from(snap("completed")), .succeeded)
        XCTAssertEqual(
            PrivacyController.EncryptMigrationState.from(snap("cancelled")), .idle)
        XCTAssertEqual(
            PrivacyController.EncryptMigrationState.from(snap("idle")), .idle)
    }
}
