import XCTest
@testable import ScreenCap

/// U12 — the Privacy pane's pure view-state rules (KTD-11) and the KTD-9/R7
/// honesty gate over its row copy.
final class PrivacySettingsPolicyTests: XCTestCase {

    /// KTD-11: ON iff `local`. `ask`/`cloud`/`both`/unknown all render OFF.
    func testKeepLocalToggleOnOnlyForLocal() {
        XCTAssertTrue(PrivacySettingsPolicy.keepLocalToggleOn(uploadDefault: "local"))
        for value in ["ask", "cloud", "both", "garbage", nil] as [String?] {
            XCTAssertFalse(
                PrivacySettingsPolicy.keepLocalToggleOn(uploadDefault: value),
                "toggle must be OFF for \(value ?? "nil")"
            )
        }
    }

    /// The OFF captions name the actual default (mirroring U6's header
    /// treatment), so a `cloud`/`both` default is never misread — and an
    /// ON→OFF write of `ask` is visible.
    func testKeepLocalCaptionNamesCurrentDefault() {
        XCTAssertTrue(PrivacySettingsPolicy.keepLocalCaption(uploadDefault: "ask").contains("ask"))
        XCTAssertTrue(PrivacySettingsPolicy.keepLocalCaption(uploadDefault: "cloud").contains("cloud"))
        XCTAssertTrue(PrivacySettingsPolicy.keepLocalCaption(uploadDefault: "both").contains("both"))
        XCTAssertTrue(PrivacySettingsPolicy.keepLocalCaption(uploadDefault: nil).contains("unknown"))
    }

    /// ON writes `local`; OFF writes `ask` — never `cloud`/`both` (KTD-11).
    func testToggleWriteValues() {
        XCTAssertEqual(PrivacySettingsPolicy.uploadDefaultValue(togglingTo: true), "local")
        XCTAssertEqual(PrivacySettingsPolicy.uploadDefaultValue(togglingTo: false), "ask")
    }

    /// KTD-9/R7: while SCR-220 is open no row claims active encryption (the
    /// design's "always on" E2EE chip), and while SCR-224 is open no row
    /// claims auto-pause works. The mask row's "always on" chip is scoped to
    /// the mask copy alone — the policy engine genuinely always runs.
    func testRowCopyCarriesNoUntrueClaims() {
        let e2ee = [
            PrivacySettingsCopy.e2eeChip, PrivacySettingsCopy.e2eeSub,
        ].joined(separator: " ").lowercased()
        XCTAssertFalse(e2ee.contains("always on"))
        XCTAssertFalse(e2ee.contains("is encrypted"))
        XCTAssertTrue(e2ee.contains("not available yet"))

        let pause = PrivacySettingsCopy.pauseSub.lowercased()
        XCTAssertTrue(pause.contains("not available yet"))

        let all = PrivacySettingsCopy.allRowStrings.joined(separator: " ").lowercased()
        XCTAssertFalse(all.contains("shared · encrypted"))
        XCTAssertFalse(all.contains("keys stay"))
    }

    /// The storage row abbreviates the home directory the design's way
    /// ("~/…"), and leaves foreign paths untouched.
    func testAbbreviateHome() {
        let home = NSHomeDirectory()
        XCTAssertEqual(
            PrivacySettingsView.abbreviateHome("\(home)/.screencap/recordings"),
            "~/.screencap/recordings"
        )
        XCTAssertEqual(PrivacySettingsView.abbreviateHome("/Volumes/ext/recs"), "/Volumes/ext/recs")
    }

    // MARK: - Storage row (SCR-228)

    /// The storage row is live now — its copy must not claim the feature is
    /// unavailable / coming soon (the old SCR-228 stub honesty gate, inverted).
    func testStorageRowMakesNoComingSoonClaim() {
        let copy = [
            PrivacySettingsCopy.storageChangeHelp,
            PrivacySettingsCopy.storageConfirmTitle,
            PrivacySettingsCopy.storageMigratingLabel,
            PrivacySettingsCopy.storageConfirmBody(target: "~/x"),
        ].joined(separator: " ").lowercased()
        XCTAssertFalse(copy.contains("coming soon"))
        XCTAssertFalse(copy.contains("not available"))
        XCTAssertFalse(copy.contains("scr-228"))
    }

    /// The confirmation body names the destructive/blocking implications so the
    /// user isn't surprised: whole-library move, recording paused, same disk.
    func testStorageConfirmBodyNamesImplications() {
        let body = PrivacySettingsCopy.storageConfirmBody(target: "~/Movies/recs").lowercased()
        XCTAssertTrue(body.contains("~/movies/recs"))
        XCTAssertTrue(body.contains("paused"))
        XCTAssertTrue(body.contains("same disk"))
    }

    /// Each machine reason code maps to a distinct, non-empty, code-free message
    /// (the fallback used when the daemon envelope omits `message`).
    func testMigrationFailureFallbackPerReason() {
        let reasons = [
            "cross_volume", "cloud_synced", "target_not_empty",
            "not_writable", "nested", "recording_active",
            "migration_in_progress", "env_override",
            "same_as_source", "source_missing",
        ]
        var seen = Set<String>()
        for reason in reasons {
            let msg = PrivacySettingsPolicy.migrationFailureFallback(reason: reason)
            XCTAssertFalse(msg.isEmpty, "empty message for \(reason)")
            XCTAssertFalse(msg.contains("_"), "\(reason) fallback leaked a code: \(msg)")
            seen.insert(msg)
        }
        XCTAssertEqual(seen.count, reasons.count, "reasons must map to distinct copy")
        // An unknown code still yields a safe generic message.
        XCTAssertFalse(
            PrivacySettingsPolicy.migrationFailureFallback(reason: "bogus").isEmpty
        )
    }
}
