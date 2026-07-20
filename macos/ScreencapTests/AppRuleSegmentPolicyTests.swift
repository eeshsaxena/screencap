import XCTest
@testable import Screencap

/// U13 — the apps-row → segmented-control mapping table (selection, locked
/// flags, notes) and the tap → CLI-transition rules, across the row shapes:
/// confirmed-allow, legacy-allow, excluded, matrix-mask, confirmation-required
/// (SCR-235 — replaces the retired matrix-exclude hard lock), and unknown app.
final class AppRuleSegmentPolicyTests: XCTestCase {

    private func app(
        bundleId: String = "com.example.app",
        contextClass: String = "unknown",
        resolvedAction: String = "allow",
        inExcludeApps: Bool = false,
        inAllowApps: Bool = false,
        isMatrixExclude: Bool = false,
        allowConfirmed: Bool? = nil,
        confirmationRequired: Bool? = nil
    ) -> InstalledApp {
        var json: [String: Any] = [
            "bundle_id": bundleId,
            "display_name": "App",
            "path": "/Applications/App.app",
            "icon_path": "",
            "context_class": contextClass,
            "classification_source": "known_app",
            "resolved_action": resolvedAction,
            "in_exclude_apps": inExcludeApps,
            "in_allow_apps": inAllowApps,
            "is_matrix_exclude": isMatrixExclude,
            "has_per_frame_overrides": false,
        ]
        if let allowConfirmed { json["allow_confirmed"] = allowConfirmed }
        if let confirmationRequired { json["confirmation_required"] = confirmationRequired }
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(InstalledApp.self, from: data)
    }

    // MARK: - Mapping table

    /// Confirmation-required (password manager, SCR-235): Block selected by
    /// the matrix default, but Record is ENABLED and routes through the
    /// consequences dialog (`.allowConfirm`) — the hard lock is retired.
    func testConfirmationRequiredRowUnlocksBehindConfirm() {
        let pm = app(
            contextClass: "password_manager", resolvedAction: "exclude",
            isMatrixExclude: true,
            allowConfirmed: false, confirmationRequired: true
        )
        let policy = AppRuleSegmentPolicy.derive(for: pm)
        XCTAssertEqual(policy.selection, .block)
        XCTAssertTrue(policy.recordEnabled)
        XCTAssertNil(policy.lockedReason)
        XCTAssertEqual(policy.note, "blocked by default · password manager")
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: pm, tapping: .record), .allowConfirm)
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: pm, tapping: .block), "already blocked")
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: pm, tapping: .mask))
    }

    /// Confirmed allow (SCR-235): Record selected regardless of class — the
    /// user's confirmed choice is authoritative; Block remains writable.
    func testConfirmedAllowRowIsRecorded() {
        let confirmed = app(
            contextClass: "password_manager", resolvedAction: "allow",
            inAllowApps: true, isMatrixExclude: true,
            allowConfirmed: true, confirmationRequired: true
        )
        let policy = AppRuleSegmentPolicy.derive(for: confirmed)
        XCTAssertEqual(policy.selection, .record)
        XCTAssertEqual(policy.note, "recorded · allowed by you")
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: confirmed, tapping: .record))
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: confirmed, tapping: .block), .excludeAdd)
    }

    /// Legacy (unconfirmed) allow entry renders its REAL floor state — the
    /// matrix mask — not a false "recorded"; Record re-confirms via the plain
    /// allow write (the CLI upgrade path).
    func testLegacyAllowRendersFloorStateHonestly() {
        let legacy = app(
            contextClass: "chat", resolvedAction: "mask_window",
            inAllowApps: true,
            allowConfirmed: false, confirmationRequired: false
        )
        let policy = AppRuleSegmentPolicy.derive(for: legacy)
        XCTAssertEqual(policy.selection, .mask)
        XCTAssertEqual(policy.note, "window masked while recording · chat")
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: legacy, tapping: .record), .allowAdd)
    }

    /// Stale-daemon decode (KTD8): a schema-v2 payload without the new keys
    /// decodes with safe defaults — unconfirmed, and confirmation-required
    /// falls back to the old `is_matrix_exclude` driver.
    func testSchemaV2PayloadDecodesWithDefaults() {
        let old = app(
            contextClass: "password_manager", resolvedAction: "exclude",
            isMatrixExclude: true
        )
        XCTAssertFalse(old.allowConfirmed)
        XCTAssertTrue(old.confirmationRequired)
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: old, tapping: .record), .allowConfirm)
    }

    /// User-excluded: Block selected and writable back — Record issues the
    /// exclude-remove vector.
    func testUserExcludedRow() {
        let excluded = app(resolvedAction: "exclude", inExcludeApps: true)
        let policy = AppRuleSegmentPolicy.derive(for: excluded)
        XCTAssertEqual(policy.selection, .block)
        XCTAssertTrue(policy.recordEnabled)
        XCTAssertNil(policy.lockedReason)
        XCTAssertEqual(policy.note, "blocked by you")
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: excluded, tapping: .record), .excludeRemove)
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: excluded, tapping: .block), "already blocked")
    }

    /// Matrix-masked (chat under internal): Mask selected-but-locked (the
    /// per-app Mask override is SCR-225 — the segment itself never accepts
    /// interaction), Record overrides via allow_apps, Block via exclude_apps.
    func testMatrixMaskedRow() {
        let masked = app(contextClass: "chat", resolvedAction: "mask_window")
        let policy = AppRuleSegmentPolicy.derive(for: masked)
        XCTAssertEqual(policy.selection, .mask)
        XCTAssertTrue(policy.recordEnabled)
        XCTAssertTrue(policy.blockEnabled)
        XCTAssertEqual(policy.note, "window masked while recording · chat")
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: masked, tapping: .record), .allowAdd)
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: masked, tapping: .block), .excludeAdd)
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: masked, tapping: .mask))
    }

    /// Allow-listed: Record selected by the user's own override; Block still
    /// writable; re-tapping Record is a no-op.
    func testAllowListedRow() {
        let allowed = app(contextClass: "email", resolvedAction: "allow", inAllowApps: true)
        let policy = AppRuleSegmentPolicy.derive(for: allowed)
        XCTAssertEqual(policy.selection, .record)
        XCTAssertEqual(policy.note, "recorded · allowed by you")
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: allowed, tapping: .record))
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: allowed, tapping: .block), .excludeAdd)
    }

    /// Unknown app (matrix allow): Record selected, Block issues exclude-add —
    /// the "Record→Block issues exclude-add argv" scenario.
    func testUnknownAppDefaultsToRecorded() {
        let unknown = app()
        let policy = AppRuleSegmentPolicy.derive(for: unknown)
        XCTAssertEqual(policy.selection, .record)
        XCTAssertEqual(policy.note, "recorded", "unknown class must not render '· unknown'")
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: unknown, tapping: .block), .excludeAdd)
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: unknown, tapping: .mask))
    }

    /// An unknown future `resolved_action` renders as recorded rather than
    /// blanking the row (the retired PrivacyBadgeStyle's defensive fallback).
    func testUnknownResolvedActionFallsBackToRecorded() {
        XCTAssertEqual(
            AppRuleSegmentPolicy.derive(for: app(resolvedAction: "hologram_redact")).selection,
            .record
        )
    }

    /// Matrix-resolved EXCLUDE on an old-CLI payload (no confirmation keys,
    /// `is_matrix_exclude` false — e.g. banking at public on schema v2):
    /// rendered blocked but writable, and Record maps to allowAdd — the CLI's
    /// verdict is the authority. On a new CLI this shape carries
    /// `confirmation_required` and routes through `.allowConfirm` instead.
    func testResolvedExcludeOldPayloadAttemptsAllow() {
        let handEdited = app(contextClass: "banking", resolvedAction: "exclude")
        let policy = AppRuleSegmentPolicy.derive(for: handEdited)
        XCTAssertEqual(policy.selection, .block)
        XCTAssertTrue(policy.recordEnabled)
        XCTAssertTrue(policy.blockEnabled)
        XCTAssertNil(policy.lockedReason)
        XCTAssertEqual(policy.note, "blocked by default · banking")
        XCTAssertEqual(AppRuleSegmentPolicy.transition(for: handEdited, tapping: .record), .allowAdd)
        XCTAssertNil(AppRuleSegmentPolicy.transition(for: handEdited, tapping: .block), "already blocked")
    }

    // MARK: - Row ordering (stable under rule changes)

    /// Live-QA regression: ranking rows by their user-toggleable rule made a
    /// just-toggled row jump groups mid-interaction, shifting every row under
    /// the cursor so the next click hit a different app. Order must be stable:
    /// only the immutable matrix-excluded rows group first; everything else is
    /// alphabetical regardless of its current rule.
    func testOrderingIsStableWhenAUserRuleChanges() {
        let bitwarden = app(
            bundleId: "com.bitwarden", contextClass: "password_manager",
            resolvedAction: "exclude", isMatrixExclude: true
        )
        let discord = app(bundleId: "com.discord", contextClass: "chat", resolvedAction: "mask_window")
        let kindle = app(bundleId: "com.kindle", resolvedAction: "allow")
        // InstalledApp uses displayName "App" for every fixture; disambiguate
        // by decoding distinct names through the ordering-relevant field.
        func named(_ base: InstalledApp, _ name: String, excluded: Bool = false) -> InstalledApp {
            let json: [String: Any] = [
                "bundle_id": base.bundleId, "display_name": name, "path": base.path,
                "icon_path": "", "context_class": base.contextClass,
                "classification_source": base.classificationSource,
                "resolved_action": base.resolvedAction,
                "in_exclude_apps": excluded, "in_allow_apps": base.inAllowApps,
                "is_matrix_exclude": base.isMatrixExclude,
                "has_per_frame_overrides": false,
            ]
            return try! JSONDecoder().decode(
                InstalledApp.self, from: try! JSONSerialization.data(withJSONObject: json)
            )
        }
        let before = AppRulesView.stableOrder([
            named(kindle, "Kindle"), named(bitwarden, "Bitwarden"), named(discord, "Discord"),
        ])
        XCTAssertEqual(before.map(\.displayName), ["Bitwarden", "Discord", "Kindle"])

        // Blocking Kindle must NOT move it — same order, new rule.
        let after = AppRulesView.stableOrder([
            named(kindle, "Kindle", excluded: true), named(bitwarden, "Bitwarden"), named(discord, "Discord"),
        ])
        XCTAssertEqual(after.map(\.displayName), ["Bitwarden", "Discord", "Kindle"])
    }

    // MARK: - Note derivation for known context classes

    func testNoteDerivationForKnownClasses() {
        XCTAssertEqual(
            AppRuleSegmentPolicy.derive(for: app(
                contextClass: "banking", resolvedAction: "mask_window"
            )).note,
            "window masked while recording · banking"
        )
        XCTAssertEqual(
            AppRuleSegmentPolicy.derive(for: app(
                contextClass: "video_call", resolvedAction: "text_redact"
            )).note,
            "text redacted while recording · video calls"
        )
    }
}
