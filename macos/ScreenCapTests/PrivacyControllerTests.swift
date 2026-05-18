import XCTest
@testable import ScreenCap

@MainActor
final class PrivacyControllerTests: XCTestCase {

    // MARK: - Test fixtures

    /// Test invoker that records every argv vector and returns canned bytes
    /// keyed by argv. Returning `nil` from `respond` falls back to an empty
    /// success envelope; throwing in `respond` lets a test exercise CLI
    /// failures.
    final class FakeInvoker: @unchecked Sendable {
        private(set) var calls: [[String]] = []
        var respond: (([String]) throws -> Data?)?

        let lock = NSLock()

        func record(_ args: [String]) {
            lock.lock(); defer { lock.unlock() }
            calls.append(args)
        }

        func invoker() -> PrivacyController.JSONInvoker {
            { [weak self] args in
                self?.record(args)
                if let data = try self?.respond?(args) {
                    return data
                }
                // Default: success-shaped envelope with no body. The controller
                // tolerates missing typed fields where they're optional.
                return Data(#"{"ok":true,"settings":{}}"#.utf8)
            }
        }
    }

    private func appsEnvelope(_ apps: [[String: Any]]) -> Data {
        let envelope: [String: Any] = [
            "ok": true,
            "schema_version": 2,
            "apps": apps,
        ]
        return try! JSONSerialization.data(withJSONObject: envelope, options: [])
    }

    private func settingsEnvelope(privacy: [String: Any]?) -> Data {
        var settings: [String: Any] = [:]
        if let privacy { settings["privacy"] = privacy }
        let envelope: [String: Any] = [
            "ok": true,
            "schema_version": 2,
            "settings": settings,
        ]
        return try! JSONSerialization.data(withJSONObject: envelope, options: [])
    }

    private func fixtureApp(bundleId: String = "com.example.foo") -> [String: Any] {
        [
            "bundle_id": bundleId,
            "display_name": "Foo",
            "path": "/Applications/Foo.app",
            "icon_path": "",
            "context_class": "browser_unverified",
            "classification_source": "bundle_id",
            "resolved_action": "allow",
            "in_exclude_apps": false,
            "in_allow_apps": false,
            "is_matrix_exclude": false,
            "has_per_frame_overrides": false,
        ]
    }

    // MARK: - refreshApps

    func testRefreshAppsDecodesEnvelope() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard args == ["apps", "--json"], let self else { return nil }
            return self.appsEnvelope([self.fixtureApp(bundleId: "com.a"), self.fixtureApp(bundleId: "com.b")])
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshApps()

        XCTAssertEqual(controller.apps.map(\.bundleId), ["com.a", "com.b"])
        XCTAssertNil(controller.lastError)
    }

    func testRefreshAppsSurfacesCLIFailureAndKeepsExistingApps() async {
        let fake = FakeInvoker()
        var firstCall = true
        fake.respond = { [weak self] _ in
            guard let self else { return nil }
            if firstCall {
                firstCall = false
                return self.appsEnvelope([self.fixtureApp()])
            }
            throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "boom"])
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshApps()  // populates
        await controller.refreshApps()  // fails

        XCTAssertEqual(controller.apps.map(\.bundleId), ["com.example.foo"])
        XCTAssertEqual(controller.lastError, "boom")
    }

    func testRefreshAppsHandlesMalformedJSON() async {
        let fake = FakeInvoker()
        fake.respond = { _ in Data("{not-json".utf8) }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshApps()

        XCTAssertNotNil(controller.lastError)
        XCTAssertTrue(controller.apps.isEmpty)
    }

    // MARK: - refreshStatus

    func testRefreshStatusDecodesPrivacyBlock() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard args == ["settings", "--json"], let self else { return nil }
            return self.settingsEnvelope(privacy: [
                "mode": "internal",
                "setup_skipped": false,
                "has_privacy_section": false,
            ])
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshStatus()

        XCTAssertEqual(controller.status?.mode, "internal")
        XCTAssertEqual(controller.status?.setupSkipped, false)
        XCTAssertEqual(controller.status?.hasPrivacySection, false)
    }

    func testRefreshStatusLeavesStatusNilOnOlderCLI() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            // Older CLI without v2 privacy block.
            self?.settingsEnvelope(privacy: nil)
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshStatus()

        XCTAssertNil(controller.status)
    }

    // MARK: - toggleExclude

    func testToggleExcludeAddsBundle() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.toggleExclude(bundleId: "com.foo", excluded: true)

        XCTAssertEqual(fake.calls, [
            ["settings", "privacy", "exclude_apps", "add", "com.foo", "--json"],
        ])
    }

    func testToggleExcludeRemovesBundle() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.toggleExclude(bundleId: "com.foo", excluded: false)

        XCTAssertEqual(fake.calls, [
            ["settings", "privacy", "exclude_apps", "remove", "com.foo", "--json"],
        ])
    }

    func testToggleExcludeSerializesConcurrentCallsForSameBundle() async {
        let fake = FakeInvoker()
        // Hold the invocation open with a continuation so two parallel calls
        // overlap on the wire — without serialization both would record.
        var continuation: CheckedContinuation<Data, Never>?
        fake.respond = { _ in
            // The fake invoker can't easily suspend, so we use a sleep + the
            // pendingToggles serialization to validate. Two concurrent toggles
            // for the same bundle should produce exactly one CLI call.
            Thread.sleep(forTimeInterval: 0.05)
            return Data(#"{"ok":true,"settings":{}}"#.utf8)
        }
        _ = continuation
        let controller = PrivacyController(invoke: fake.invoker())

        async let a: Void = controller.toggleExclude(bundleId: "com.foo", excluded: true)
        async let b: Void = controller.toggleExclude(bundleId: "com.foo", excluded: true)
        _ = await (a, b)

        XCTAssertEqual(fake.calls.count, 1, "expected serialization to drop the duplicate; got \(fake.calls)")
    }

    // MARK: - markSetupComplete

    func testMarkSetupCompleteWritesAndRefreshes() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args == ["settings", "--json"] {
                return self.settingsEnvelope(privacy: [
                    "mode": "internal",
                    "setup_skipped": true,
                    "has_privacy_section": true,
                ])
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.markSetupComplete()

        XCTAssertEqual(fake.calls.prefix(2).map { $0 }, [
            ["settings", "privacy", "setup_skipped", "set", "true", "--json"],
            ["settings", "--json"],
        ])
        XCTAssertEqual(controller.status?.setupSkipped, true)
    }

    // MARK: - ensureFirstLaunchModeWritten

    func testEnsureFirstLaunchModeWriteFiresWhenSectionMissing() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args == ["settings", "--json"] {
                return self.settingsEnvelope(privacy: [
                    "mode": "internal",
                    "setup_skipped": false,
                    "has_privacy_section": false,
                ])
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.ensureFirstLaunchModeWritten()

        XCTAssertEqual(fake.calls, [
            ["settings", "--json"],
            ["settings", "privacy", "mode", "set", "internal", "--json"],
        ])
    }

    func testEnsureFirstLaunchModeWriteSkipsWhenSectionPresent() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args == ["settings", "--json"] {
                return self.settingsEnvelope(privacy: [
                    "mode": "public",
                    "setup_skipped": true,
                    "has_privacy_section": true,
                ])
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.ensureFirstLaunchModeWritten()

        XCTAssertEqual(fake.calls, [["settings", "--json"]])
    }

    func testEnsureFirstLaunchModeWriteLatchesOnRepeatCalls() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args == ["settings", "--json"] {
                return self.settingsEnvelope(privacy: [
                    "mode": "internal",
                    "setup_skipped": false,
                    "has_privacy_section": false,
                ])
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.ensureFirstLaunchModeWritten()
        await controller.ensureFirstLaunchModeWritten()

        XCTAssertEqual(fake.calls.count, 2, "second invocation must short-circuit")
    }

    // MARK: - bannerActive derivation

    func testBannerInactiveUntilStatusLoads() {
        let controller = PrivacyController(invoke: { _ in Data() })
        XCTAssertFalse(controller.bannerActive)
    }

    func testBannerActiveWhenSetupNotSkipped() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            self?.settingsEnvelope(privacy: [
                "mode": "internal",
                "setup_skipped": false,
                "has_privacy_section": true,
            ])
        }
        let controller = PrivacyController(invoke: fake.invoker())
        await controller.refreshStatus()
        XCTAssertTrue(controller.bannerActive)
    }

    func testBannerInactiveWhenSetupSkipped() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            self?.settingsEnvelope(privacy: [
                "mode": "internal",
                "setup_skipped": true,
                "has_privacy_section": true,
            ])
        }
        let controller = PrivacyController(invoke: fake.invoker())
        await controller.refreshStatus()
        XCTAssertFalse(controller.bannerActive)
    }
}
