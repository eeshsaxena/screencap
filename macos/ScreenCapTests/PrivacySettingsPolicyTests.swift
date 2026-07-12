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
            // ANY state until the Stage 3 default flip. SCR-253 U8 retires the
            // now-false Stage-1 "only this Mac can decrypt" claim (the key syncs
            // to the user's other Macs), so it too is forbidden in every state.
            let e2ee = [
                PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: state),
                PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: state),
                PrivacySettingsPolicy.e2eeHelp(cloudE2EEEnabled: state),
                PrivacySettingsCopy.e2eeConfirmTitle,
                PrivacySettingsCopy.e2eeConfirmBody,
            ].joined(separator: " ").lowercased()
            XCTAssertFalse(e2ee.contains("always on"), "state \(String(describing: state))")
            XCTAssertFalse(e2ee.contains("only this mac"), "state \(String(describing: state))")
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

    /// On: the beta copy claims multi-device encryption truthfully — scoped to
    /// the user's own Macs, in iCloud-Keychain-honest wording (SCR-253 U8,
    /// KTD-6: "neither we nor Apple can read") — and names the no-recovery
    /// limit (KD7). The retired Stage-1 "only this Mac can decrypt" claim, now
    /// false because the key syncs, must be gone.
    func testE2EEOnStateClaimsMultiDeviceEncryptionAndNamesNoRecovery() {
        XCTAssertEqual(
            PrivacySettingsPolicy.e2eeTapOutcome(cloudE2EEEnabled: true), .disable
        )
        XCTAssertTrue(PrivacySettingsPolicy.e2eeToggleOn(cloudE2EEEnabled: true))
        XCTAssertEqual(PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: true), "beta")

        let caption = PrivacySettingsPolicy.e2eeCaption(cloudE2EEEnabled: true).lowercased()
        XCTAssertTrue(caption.contains("encrypted"))
        XCTAssertTrue(caption.contains("your macs"))
        XCTAssertTrue(caption.contains("icloud keychain"))
        XCTAssertTrue(caption.contains("neither we nor apple"))
        XCTAssertTrue(caption.contains("no recovery"))
        XCTAssertFalse(caption.contains("only this mac can decrypt"))  // retired (U8)
    }

    /// R4: the disclosure that gates the off→on flip names the Stage-2 custody
    /// and limits plainly before the user commits (SCR-253 U8, KTD-6): the key
    /// syncs to the user's Macs via iCloud Keychain, neither we nor Apple can
    /// read it, and there is no recovery — losing access to all their Macs
    /// loses access to the encrypted copies. The false "only this Mac" claim is
    /// gone.
    func testE2EEConfirmBodyNamesCustodyAndLimits() {
        let body = PrivacySettingsCopy.e2eeConfirmBody.lowercased()
        XCTAssertTrue(body.contains("icloud keychain"))
        XCTAssertTrue(body.contains("neither we nor apple"))
        XCTAssertTrue(body.contains("no recovery"))
        XCTAssertTrue(body.contains("all your macs"))
        XCTAssertTrue(body.contains("lose access"))
        XCTAssertFalse(body.contains("only this mac can decrypt"))  // retired (U8)
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
