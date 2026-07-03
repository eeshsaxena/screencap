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
}
