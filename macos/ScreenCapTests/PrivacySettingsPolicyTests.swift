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

    /// KTD-9/KD7 honesty gate, rewritten for SCR-220 U4 (the E2EE row is now
    /// a live opt-in beta toggle, so its copy is state-keyed): claims that
    /// unlock only in later stages — "always on" on the E2EE row (Stage 3),
    /// "shared · encrypted" (SCR-221) — and claims that are never true —
    /// "keys stay" (KD3), "we can't watch" — stay forbidden in EVERY E2EE
    /// state. SCR-224's row still states auto-pause is absent.
    func testRowCopyCarriesNoUntrueClaims() {
        for state in [nil, false, true] as [Bool?] {
            let all = PrivacySettingsCopy.allRowStrings(cloudE2EEEnabled: state)
                .joined(separator: " ").lowercased()
            XCTAssertFalse(all.contains("shared · encrypted"), "state \(String(describing: state))")
            XCTAssertFalse(all.contains("keys stay"), "state \(String(describing: state))")
            XCTAssertFalse(all.contains("we can't watch"), "state \(String(describing: state))")

            // "always on" is scoped to the mask chip alone (the policy engine
            // genuinely always runs) — the E2EE strings must not carry it in
            // ANY state until the Stage 3 default flip.
            let e2ee = [
                PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: state),
                PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: state),
                PrivacySettingsPolicy.e2eeHelp(cloudE2EEEnabled: state),
                PrivacySettingsCopy.e2eeConfirmTitle,
                PrivacySettingsCopy.e2eeConfirmBody,
            ].joined(separator: " ").lowercased()
            XCTAssertFalse(e2ee.contains("always on"), "state \(String(describing: state))")
        }

        let pause = PrivacySettingsCopy.pauseSub.lowercased()
        XCTAssertTrue(pause.contains("not available yet"))
    }

    // MARK: - E2EE row states (SCR-220 U4, KTD-8/KTD-9)

    /// nil flag (older CLI without the `e2ee` verb, KTD-8): the row stays in
    /// the locked stub presentation — no toggle, no beta label, no claim that
    /// current uploads are (or can be) encrypted.
    func testE2EENilStateKeepsLockedStubPresentation() {
        XCTAssertEqual(
            PrivacySettingsPolicy.e2eeTapOutcome(cloudE2EEEnabled: nil), .locked
        )
        XCTAssertFalse(PrivacySettingsPolicy.e2eeToggleOn(cloudE2EEEnabled: nil))

        let copy = [
            PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: nil),
            PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: nil),
        ].joined(separator: " ").lowercased()
        XCTAssertTrue(copy.contains("not available yet"))
        XCTAssertFalse(copy.contains("is encrypted"))
        XCTAssertFalse(copy.contains("beta"))
    }

    /// Off: an opt-in row labeled "beta" whose copy makes no claim that
    /// current uploads are encrypted — it says plainly they are not.
    func testE2EEOffStateOptInCopyMakesNoEncryptionClaim() {
        XCTAssertEqual(
            PrivacySettingsPolicy.e2eeTapOutcome(cloudE2EEEnabled: false), .showDisclosure
        )
        XCTAssertFalse(PrivacySettingsPolicy.e2eeToggleOn(cloudE2EEEnabled: false))
        XCTAssertEqual(PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: false), "beta")

        let caption = PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: false).lowercased()
        XCTAssertFalse(caption.contains("is encrypted"))
        XCTAssertFalse(caption.contains("not available"), "the capability IS available now")
        XCTAssertTrue(caption.contains("off"))
        XCTAssertTrue(caption.contains("turn on"))
    }

    /// On: the beta copy claims per-Mac encryption truthfully — scoped to
    /// this Mac's cloud copies — and names the no-recovery limit (KD7).
    func testE2EEOnStateClaimsPerMacEncryptionAndNamesNoRecovery() {
        XCTAssertEqual(
            PrivacySettingsPolicy.e2eeTapOutcome(cloudE2EEEnabled: true), .disable
        )
        XCTAssertTrue(PrivacySettingsPolicy.e2eeToggleOn(cloudE2EEEnabled: true))
        XCTAssertEqual(PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: true), "beta")

        let caption = PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: true).lowercased()
        XCTAssertTrue(caption.contains("encrypted"))
        XCTAssertTrue(caption.contains("only this mac can decrypt"))
        XCTAssertTrue(caption.contains("no recovery"))
    }

    /// R4: the disclosure that gates the off→on flip names all three limits
    /// plainly before the user commits — only this Mac can decrypt, no
    /// recovery exists, losing this Mac loses access to the encrypted copies.
    func testE2EEConfirmBodyNamesAllThreeLimits() {
        let body = PrivacySettingsCopy.e2eeConfirmBody.lowercased()
        XCTAssertTrue(body.contains("only this mac can decrypt"))
        XCTAssertTrue(body.contains("no recovery"))
        XCTAssertTrue(body.contains("lose this mac"))
        XCTAssertTrue(body.contains("lose access"))
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
