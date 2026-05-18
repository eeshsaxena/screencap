import XCTest
@testable import ScreenCap

/// Banner state-machine + first-launch sequencing tests for SCR-17 / U5.
///
/// The banner view itself (`FirstRunPrivacyBanner`) is a stateless wrapper
/// over two closures; its rendering correctness is covered by SwiftUI
/// previews. These tests exercise the controller-side state transitions
/// and the dual-path "markSetupComplete" semantics (banner CTA AND pane
/// visit both clear).
@MainActor
final class FirstRunPrivacyBannerTests: XCTestCase {

    // MARK: - Fixtures

    /// Reusable invoker scaffolding shared across tests — records argv
    /// vectors and dispatches `settings --json` / `apps --json` against the
    /// supplied state machine.
    final class Scripted: @unchecked Sendable {
        var calls: [[String]] = []
        var privacyBlock: [String: Any]?

        init(privacyBlock: [String: Any]? = nil) {
            self.privacyBlock = privacyBlock
        }

        static func settingsEnvelope(privacy: [String: Any]?) -> Data {
            var settings: [String: Any] = [:]
            if let privacy { settings["privacy"] = privacy }
            return try! JSONSerialization.data(withJSONObject: [
                "ok": true,
                "schema_version": 2,
                "settings": settings,
            ], options: [])
        }

        static func appsEnvelope() -> Data {
            try! JSONSerialization.data(withJSONObject: [
                "ok": true,
                "schema_version": 2,
                "apps": [],
            ], options: [])
        }

        func invoker() -> PrivacyController.JSONInvoker {
            { [weak self] args in
                guard let self else { return Data() }
                self.calls.append(args)
                if args == ["apps", "--json"] {
                    return Scripted.appsEnvelope()
                }
                if args == ["settings", "--json"] {
                    return Scripted.settingsEnvelope(privacy: self.privacyBlock)
                }
                // settings privacy <field> <op> <value> --json — pretend to
                // mutate so the next status read reflects it.
                if args.contains("setup_skipped"), args.contains("true") {
                    self.privacyBlock?["setup_skipped"] = true
                }
                if args.contains("mode"), let idx = args.firstIndex(of: "set"), idx + 1 < args.count {
                    let value = args[idx + 1]
                    if self.privacyBlock == nil {
                        self.privacyBlock = [
                            "mode": value,
                            "setup_skipped": false,
                            "has_privacy_section": true,
                        ]
                    } else {
                        self.privacyBlock?["mode"] = value
                        self.privacyBlock?["has_privacy_section"] = true
                    }
                }
                return Data(#"{"ok":true,"settings":{}}"#.utf8)
            }
        }
    }

    // MARK: - First-launch sequencing (R8)

    func testFreshInstallWritesFailClosedModeBeforeBanner() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "internal",
            "setup_skipped": false,
            "has_privacy_section": false,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())

        await controller.ensureFirstLaunchModeWritten()
        await controller.refreshStatus()

        XCTAssertTrue(scripted.calls.contains(
            ["settings", "privacy", "mode", "set", "internal", "--json"]
        ))
        // After the write, has_privacy_section flips to true in the fixture.
        XCTAssertEqual(controller.status?.hasPrivacySection, true)
    }

    func testExistingPrivacySectionSkipsFailClosedWrite() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "public",
            "setup_skipped": true,
            "has_privacy_section": true,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())

        await controller.ensureFirstLaunchModeWritten()

        XCTAssertFalse(scripted.calls.contains(
            ["settings", "privacy", "mode", "set", "internal", "--json"]
        ))
    }

    // MARK: - Banner state machine (R5)

    func testBannerHiddenWhileStatusLoading() {
        let controller = PrivacyController(invoke: { _ in Data() })
        XCTAssertNil(controller.status)
        XCTAssertFalse(controller.bannerActive)
    }

    func testBannerShownWhenSetupNotSkipped() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "internal",
            "setup_skipped": false,
            "has_privacy_section": true,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())
        await controller.refreshStatus()
        XCTAssertTrue(controller.bannerActive)
    }

    func testBannerHiddenAfterSetupSkipped() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "internal",
            "setup_skipped": true,
            "has_privacy_section": true,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())
        await controller.refreshStatus()
        XCTAssertFalse(controller.bannerActive)
    }

    // MARK: - CTA flows (R6)

    func testDismissCTACallsMarkSetupComplete() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "internal",
            "setup_skipped": false,
            "has_privacy_section": true,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())
        await controller.refreshStatus()
        XCTAssertTrue(controller.bannerActive)

        await controller.markSetupComplete()

        XCTAssertEqual(controller.status?.setupSkipped, true)
        XCTAssertFalse(controller.bannerActive)
        XCTAssertTrue(scripted.calls.contains(
            ["settings", "privacy", "setup_skipped", "set", "true", "--json"]
        ))
    }

    func testRapidDoubleDismissDoesNotDoubleWrite() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "internal",
            "setup_skipped": false,
            "has_privacy_section": true,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())
        await controller.refreshStatus()

        async let a: Void = controller.markSetupComplete()
        async let b: Void = controller.markSetupComplete()
        _ = await (a, b)

        let writes = scripted.calls.filter {
            $0 == ["settings", "privacy", "setup_skipped", "set", "true", "--json"]
        }
        // Controller serializes overlapping markSetupComplete calls — the CLI
        // write is idempotent, but firing it twice would acquire the advisory
        // flock twice and emit duplicate telemetry.
        XCTAssertEqual(writes.count, 1)
        XCTAssertFalse(controller.bannerActive)
    }

    // MARK: - Integration: first-launch flow end-to-end

    func testFirstLaunchFlowEndToEnd() async {
        let scripted = Scripted(privacyBlock: [
            "mode": "internal",
            "setup_skipped": false,
            "has_privacy_section": false,
        ])
        let controller = PrivacyController(invoke: scripted.invoker())

        // App launch sequence (mirrors ScreenCapApp.task ordering).
        await controller.ensureFirstLaunchModeWritten()
        await controller.refreshStatus()
        await controller.refreshApps()

        // Mode was written → has_privacy_section is now true; setup_skipped
        // still false → banner shows.
        XCTAssertEqual(controller.status?.mode, "internal")
        XCTAssertEqual(controller.status?.hasPrivacySection, true)
        XCTAssertEqual(controller.status?.setupSkipped, false)
        XCTAssertTrue(controller.bannerActive)

        // User clicks "Review what's captured" → markSetupComplete then nav.
        await controller.markSetupComplete()

        XCTAssertFalse(controller.bannerActive)
        XCTAssertEqual(controller.status?.setupSkipped, true)
    }
}
