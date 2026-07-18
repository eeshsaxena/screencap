import XCTest
@testable import ScreenCap

/// U4 — the sidebar's pure model: which rows route, which are disabled until a
/// later unit / deferred ticket, and the footer storage math. Rendering is
/// verified by build-and-run; the routing / stub / footer *rules* are pinned here.
final class ShellSidebarModelTests: XCTestCase {

    private func item(_ items: [ShellNavItem], _ id: String) -> ShellNavItem {
        items.first { $0.id == id }!
    }

    // MARK: - Routing + enablement

    /// R1 — the primary nav is exactly Days · Tasks · Clips · Chat, in order,
    /// with no Library/Journal/Collections rows.
    func testPrimaryNavIsDaysTasksClipsChat() {
        XCTAssertEqual(ShellSidebarModel.primaryNav.map(\.id), ["days", "tasks", "clips", "chat"])
        XCTAssertEqual(
            ShellSidebarModel.primaryNav.map(\.route),
            [.days, .tasks, .clips, .chat]
        )
        XCTAssertTrue(ShellSidebarModel.primaryNav.allSatisfy(\.isEnabled))
    }

    func testDaysRoutesAndIsEnabled() {
        let days = item(ShellSidebarModel.primaryNav, "days")
        XCTAssertEqual(days.route, .days)
        XCTAssertTrue(days.isEnabled)
        XCTAssertNil(days.helpText, "an enabled row has no coming-soon tooltip")
    }

    /// Tasks and Clips route now (to honest placeholders until U6/U11 land).
    func testTasksAndClipsRouteAndAreEnabled() {
        let tasks = item(ShellSidebarModel.primaryNav, "tasks")
        XCTAssertEqual(tasks.route, .tasks)
        XCTAssertTrue(tasks.isEnabled)
        let clips = item(ShellSidebarModel.primaryNav, "clips")
        XCTAssertEqual(clips.route, .clips)
        XCTAssertTrue(clips.isEnabled)
    }

    /// KTD-4 (account-sheet U5): Account is pinned FIRST in the SETTINGS group
    /// — it is the paid-only gate's home surface, and burying it slows a
    /// lapsed user's path back to a resolvable state.
    func testAccountIsFirstInSettingsGroup() {
        let first = ShellSidebarModel.settingsNav.first
        XCTAssertEqual(first?.id, "account")
        XCTAssertEqual(first?.route, .account)
        XCTAssertEqual(first?.isEnabled, true)
    }

    func testPrivacyRoutesAndIsEnabled() {
        let privacy = item(ShellSidebarModel.settingsNav, "privacy")
        XCTAssertEqual(privacy.route, .privacy)
        XCTAssertTrue(privacy.isEnabled)
        XCTAssertNil(privacy.helpText)
    }

    func testAppRulesRoutesAndIsEnabled() {
        let row = item(ShellSidebarModel.settingsNav, "appRules")
        XCTAssertEqual(row.route, .appRules)
        XCTAssertTrue(row.isEnabled, "App rules routes to its pane since U13")
        XCTAssertNil(row.helpText)
    }

    // MARK: - Footer storage math

    func testStorageFooterSumsBytesAndFormatsGB() {
        let footer = ShellSidebarModel.storageFooter([
            rec(sizeBytes: 2 * 1_073_741_824),
            rec(sizeBytes: 2_684_354_560),  // 2.5 GB
        ])
        XCTAssertEqual(footer.byteText, "4.5 GB")
        XCTAssertTrue(footer.allLocal, "no recording uploaded → all local")
        XCTAssertEqual(footer.line, "all local · 4.5 GB on this Mac")
    }

    func testStorageFooterDropsAllLocalWhenAnyUploaded() {
        let footer = ShellSidebarModel.storageFooter([
            rec(sizeBytes: 1_073_741_824),
            rec(sizeBytes: 1_073_741_824, uploaded: true),
        ])
        XCTAssertFalse(footer.allLocal)
        XCTAssertEqual(footer.line, "2.0 GB on this Mac")
    }

    func testStorageFooterFormatsSmallLibraries() {
        XCTAssertEqual(ShellSidebarModel.formatStorage(0), "0 KB")
        XCTAssertEqual(ShellSidebarModel.formatStorage(2048), "2 KB")
        XCTAssertEqual(ShellSidebarModel.formatStorage(5 * 1_048_576), "5 MB")
        XCTAssertEqual(ShellSidebarModel.formatStorage(1_073_741_824), "1.0 GB")
    }

    func testStorageFooterEmptyIsAllLocalZero() {
        let footer = ShellSidebarModel.storageFooter([])
        XCTAssertTrue(footer.allLocal)
        XCTAssertEqual(footer.line, "all local · 0 KB on this Mac")
    }

    // MARK: - Helper

    private func rec(sizeBytes: Int, uploaded: Bool = false) -> RecordingSummary {
        let json = """
        {"name":"r","date":"2026-07-03","duration":"0:10","size_mb":"x",
         "has_audio":false,"transcribed":false,"uploaded":\(uploaded),"is_stub":false,
         "chunks_total":0,"chunks_uploaded":0,"intent":null,
         "started_at":1.0,"duration_seconds":10.0,"drops":null,"size_bytes":\(sizeBytes)}
        """
        return try! JSONDecoder().decode(RecordingSummary.self, from: Data(json.utf8))
    }

    // MARK: - SCR-239 (U11) local-model hint visibility

    func testLocalModelHintVisibilityRules() {
        // Shown: on-device active, no cloud fallback, no model, not dismissed.
        XCTAssertTrue(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: nil,
            downloadedInstalled: false, dismissed: false))
        // Hidden once installed.
        XCTAssertFalse(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: nil,
            downloadedInstalled: true, dismissed: false))
        // Hidden once dismissed (R7 — no re-prompt).
        XCTAssertFalse(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: nil,
            downloadedInstalled: false, dismissed: true))
        // Hidden for a legacy cloud value in the provider slot.
        XCTAssertFalse(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "gemini", cloudProvider: nil,
            downloadedInstalled: false, dismissed: false))
        // Hidden when settings unknown (nil provider).
        XCTAssertFalse(ShellSidebarModel.shouldShowLocalModelHint(
            provider: nil, cloudProvider: nil,
            downloadedInstalled: false, dismissed: false))
    }

    /// R13 (U6) — under the two-slot semantics a BYO pick sets `cloud_provider`
    /// and leaves `provider == "on-device"`, so the hint must check the cloud
    /// slot explicitly: never nag to download a local model while a cloud
    /// provider is the rendered selection.
    func testLocalModelHintHiddenWhileCloudProviderSelected() {
        XCTAssertFalse(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: "anthropic",
            downloadedInstalled: false, dismissed: false))
    }

    /// The cloud slot is normalized the way IntelligenceSelectionModel reads it
    /// back: nil, empty/whitespace, and the CLI's clear-literal "none" all mean
    /// unset — none of them hides the hint.
    func testLocalModelHintTreatsNoneAndEmptyCloudProviderAsUnset() {
        XCTAssertTrue(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: "none",
            downloadedInstalled: false, dismissed: false))
        XCTAssertTrue(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: "",
            downloadedInstalled: false, dismissed: false))
        XCTAssertTrue(ShellSidebarModel.shouldShowLocalModelHint(
            provider: "on-device", cloudProvider: "  ",
            downloadedInstalled: false, dismissed: false))
    }
}
