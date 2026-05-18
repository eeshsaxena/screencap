import XCTest
@testable import ScreenCap

final class PrivacyBadgeStyleTests: XCTestCase {

    private func app(
        bundleId: String = "com.example.foo",
        resolvedAction: String = "allow",
        inExcludeApps: Bool = false,
        inAllowApps: Bool = false,
        isMatrixExclude: Bool = false,
        hasPerFrameOverrides: Bool = false
    ) -> InstalledApp {
        // InstalledApp has no memberwise init; build via Decodable to avoid
        // duplicating a parallel constructor that would drift from the model.
        let payload: [String: Any] = [
            "bundle_id": bundleId,
            "display_name": "Foo",
            "path": "/Applications/Foo.app",
            "icon_path": "",
            "context_class": "browser_unverified",
            "classification_source": "bundle_id",
            "resolved_action": resolvedAction,
            "in_exclude_apps": inExcludeApps,
            "in_allow_apps": inAllowApps,
            "is_matrix_exclude": isMatrixExclude,
            "has_per_frame_overrides": hasPerFrameOverrides,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload, options: [])
        return try! JSONDecoder().decode(InstalledApp.self, from: data)
    }

    // MARK: - Matrix-exclude beats every other signal

    func testMatrixExcludeReturnsAlwaysBlockedAndDisablesToggle() {
        let style = PrivacyBadgeStyle.derive(for: app(isMatrixExclude: true))
        XCTAssertEqual(style.text, "Always blocked (security)")
        XCTAssertTrue(style.toggleDisabled)
        XCTAssertEqual(style.kind, .blockedBySecurity)
    }

    func testMatrixExcludeWinsOverUserExclude() {
        // User exclude added redundantly — matrix-exclude still drives display.
        let style = PrivacyBadgeStyle.derive(for: app(
            inExcludeApps: true,
            isMatrixExclude: true
        ))
        XCTAssertEqual(style.text, "Always blocked (security)")
        XCTAssertTrue(style.toggleDisabled)
    }

    // MARK: - User exclude

    func testUserExcludeRendersExcludedByYou() {
        let style = PrivacyBadgeStyle.derive(for: app(
            resolvedAction: "exclude",
            inExcludeApps: true
        ))
        XCTAssertEqual(style.text, "Excluded by you")
        XCTAssertFalse(style.toggleDisabled)
        XCTAssertTrue(style.toggleOn)
        XCTAssertEqual(style.kind, .excludedByUser)
    }

    // MARK: - User allow

    func testUserAllowRendersCapturedAllowedByYou() {
        let style = PrivacyBadgeStyle.derive(for: app(
            resolvedAction: "allow",
            inAllowApps: true
        ))
        XCTAssertEqual(style.text, "Captured (allowed by you)")
        XCTAssertFalse(style.toggleDisabled)
        XCTAssertFalse(style.toggleOn)
        XCTAssertEqual(style.kind, .captured)
    }

    // MARK: - Matrix-resolved actions

    func testMaskWindowRendersWindowMasked() {
        let style = PrivacyBadgeStyle.derive(for: app(resolvedAction: "mask_window"))
        XCTAssertEqual(style.text, "Captured (window masked)")
        XCTAssertFalse(style.toggleDisabled)
        XCTAssertFalse(style.toggleOn)
    }

    func testTextRedactRendersTextRedacted() {
        let style = PrivacyBadgeStyle.derive(for: app(resolvedAction: "text_redact"))
        XCTAssertEqual(style.text, "Captured (text redacted)")
    }

    func testAllowRendersCaptured() {
        let style = PrivacyBadgeStyle.derive(for: app(resolvedAction: "allow"))
        XCTAssertEqual(style.text, "Captured")
        XCTAssertNil(style.tooltip)
    }

    // MARK: - Per-frame overrides decoration

    func testAllowRowWithPerFrameOverridesGetsAsteriskAndTooltip() {
        let style = PrivacyBadgeStyle.derive(for: app(
            resolvedAction: "allow",
            hasPerFrameOverrides: true
        ))
        XCTAssertEqual(style.text, "Captured*")
        XCTAssertNotNil(style.tooltip)
    }

    func testMaskedRowWithPerFrameOverridesIsNotDecorated() {
        // Mask-resolved rows already disclose the masking; per-frame override
        // overlap is not informative there.
        let style = PrivacyBadgeStyle.derive(for: app(
            resolvedAction: "mask_window",
            hasPerFrameOverrides: true
        ))
        XCTAssertEqual(style.text, "Captured (window masked)")
        XCTAssertNil(style.tooltip)
    }
}
