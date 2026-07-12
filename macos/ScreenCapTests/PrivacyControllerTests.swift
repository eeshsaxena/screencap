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

    private func settingsEnvelope(
        privacy: [String: Any]?,
        uploadDefault: String? = nil,
        recordingsDir: String? = nil,
        cloudE2EEEnabled: Bool? = nil
    ) -> Data {
        var settings: [String: Any] = [:]
        if let privacy { settings["privacy"] = privacy }
        if let uploadDefault { settings["upload_default"] = uploadDefault }
        if let recordingsDir { settings["recordings_dir"] = recordingsDir }
        if let cloudE2EEEnabled { settings["cloud_e2ee_enabled"] = cloudE2EEEnabled }
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

    func testToggleExcludeAddsBundleAndRefreshes() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.toggleExclude(bundleId: "com.foo", excluded: true)

        // Write followed by refresh — converges optimistic UI to disk truth.
        XCTAssertEqual(fake.calls, [
            ["settings", "privacy", "exclude_apps", "add", "com.foo", "--json"],
            ["apps", "--json"],
        ])
    }

    func testToggleExcludeRemovesBundleAndRefreshes() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.toggleExclude(bundleId: "com.foo", excluded: false)

        XCTAssertEqual(fake.calls, [
            ["settings", "privacy", "exclude_apps", "remove", "com.foo", "--json"],
            ["apps", "--json"],
        ])
    }

    // MARK: - confirmAllow (SCR-235)

    /// The confirmed-allow write carries the `--confirm-sensitive` flag —
    /// the argv contract the Python confirmation gate keys on — and only
    /// fires AFTER the view's consequences dialog (the controller itself
    /// never prompts).
    func testConfirmAllowPassesConfirmFlagAndRefreshes() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.confirmAllow(bundleId: "com.1password.1password")

        XCTAssertEqual(fake.calls, [
            ["settings", "privacy", "allow_apps", "add", "com.1password.1password",
             "--confirm-sensitive", "--json"],
            ["apps", "--json"],
        ])
    }

    func testToggleExcludeRefreshesEvenOnWriteFailure() async {
        var firstWriteFailed = false
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args.contains("exclude_apps") {
                firstWriteFailed = true
                throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "boom"])
            }
            if args == ["apps", "--json"] {
                // Make the refresh fail too so the user can see the error.
                if firstWriteFailed {
                    throw NSError(domain: "test", code: 2,
                                  userInfo: [NSLocalizedDescriptionKey: "refresh after failed write"])
                }
                return self.appsEnvelope([])
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.toggleExclude(bundleId: "com.foo", excluded: true)

        // Refresh must fire even after a failed write — otherwise the row's
        // optimistic toggle drifts from disk truth.
        XCTAssertTrue(fake.calls.contains(["apps", "--json"]))
    }

    /// Pins the argv-array (no-shell) contract: a malicious `bundle_id`
    /// containing shell metacharacters must land as a single argv element,
    /// not as separate shell tokens. A regression to `/bin/sh -c` wrapping
    /// would silently introduce command injection — this test is the canary.
    func testToggleExcludePreservesBundleIdAsSingleArgvElement() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())
        let hostile = "com.foo; rm -rf $HOME `id`"

        await controller.toggleExclude(bundleId: hostile, excluded: true)

        // First call is the write; the bundle_id stays as a single argv
        // element regardless of the metacharacters.
        XCTAssertEqual(
            fake.calls.first,
            ["settings", "privacy", "exclude_apps", "add", hostile, "--json"]
        )
    }

    func testToggleExcludeSerializesConcurrentCallsForSameBundle() async {
        let fake = FakeInvoker()
        fake.respond = { _ in
            Thread.sleep(forTimeInterval: 0.05)
            return Data(#"{"ok":true,"settings":{}}"#.utf8)
        }
        let controller = PrivacyController(invoke: fake.invoker())

        async let a: Void = controller.toggleExclude(bundleId: "com.foo", excluded: true)
        async let b: Void = controller.toggleExclude(bundleId: "com.foo", excluded: true)
        _ = await (a, b)

        // Both calls share `pendingToggles`, so only one CLI write fires.
        // Filter to just the exclude_apps writes so the apps refresh that
        // follows doesn't mask the serialization signal.
        let writes = fake.calls.filter { $0.contains("exclude_apps") }
        XCTAssertEqual(writes.count, 1, "expected serialization to drop duplicate writes; got \(writes)")
    }

    // MARK: - markSetupComplete

    /// Optimistic local flip of `setupSkipped` keeps the banner dismissed
    /// even if the post-write `refreshStatus()` fails. Without this, a CLI
    /// hiccup right after a successful `setup_skipped = true` write would
    /// resurrect the dismissed banner — confusing the user.
    func testMarkSetupCompleteHidesBannerEvenIfRefreshFails() async {
        var refreshShouldFail = false
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args == ["settings", "--json"] {
                if refreshShouldFail {
                    throw NSError(domain: "test", code: 1,
                                  userInfo: [NSLocalizedDescriptionKey: "post-write refresh failed"])
                }
                return self.settingsEnvelope(privacy: [
                    "mode": "internal",
                    "setup_skipped": false,
                    "has_privacy_section": true,
                ])
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())
        await controller.refreshStatus()
        XCTAssertTrue(controller.bannerActive)

        refreshShouldFail = true
        await controller.markSetupComplete()

        XCTAssertEqual(controller.status?.setupSkipped, true,
                       "optimistic update must survive refresh failure")
        XCTAssertFalse(controller.bannerActive)
    }

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

    func testEnsureFirstLaunchModeWriteLatchesAfterSuccessfulWrite() async {
        var sectionExists = false
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if args == ["settings", "--json"] {
                return self.settingsEnvelope(privacy: [
                    "mode": "internal",
                    "setup_skipped": false,
                    "has_privacy_section": sectionExists,
                ])
            }
            if args.contains("mode") && args.contains("internal") {
                sectionExists = true
            }
            return nil
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.ensureFirstLaunchModeWritten()
        let countAfterFirst = fake.calls.count
        await controller.ensureFirstLaunchModeWritten()

        XCTAssertEqual(fake.calls.count, countAfterFirst,
                       "latched after success — second call must short-circuit")
    }

    /// The latch MUST NOT engage on failure. A transient CLI error mid-launch
    /// otherwise strands the user without the fail-closed mode write for the
    /// rest of the app session — the @StateObject controller survives window
    /// close, so any retry path (next .task fire) would silently no-op.
    func testEnsureFirstLaunchModeWriteRetriesAfterFailure() async {
        var failNext = true
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard let self else { return nil }
            if failNext {
                failNext = false
                throw NSError(domain: "test", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "transient CLI failure"])
            }
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
        // First attempt fails — error surfaces, latch stays open.
        XCTAssertEqual(controller.lastError, "transient CLI failure")

        await controller.ensureFirstLaunchModeWritten()
        // Second attempt succeeds and actually issues the fail-closed write.
        XCTAssertTrue(fake.calls.contains(
            ["settings", "privacy", "mode", "set", "internal", "--json"]
        ))
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

    // MARK: - setUploadDefault (U12, KTD-11)

    /// The upload-default write ships exactly the CLI vector the Python side
    /// expects — `settings --set upload_default=<value> --json`. "local" is
    /// U11's storage-pick write; "ask" is U12's keep-local OFF write (KTD-11).
    func testSetUploadDefaultIssuesExpectedArgv() async {
        for value in ["local", "ask"] {
            let fake = FakeInvoker()
            let controller = PrivacyController(invoke: fake.invoker())

            let ok = await controller.setUploadDefault(value)

            XCTAssertTrue(ok)
            XCTAssertEqual(fake.calls, [["settings", "--set", "upload_default=\(value)", "--json"]])
            XCTAssertEqual(controller.uploadDefault, value)
        }
    }

    /// Optimistic flip + revert-on-failure (KTD-11): a nonzero exit restores
    /// the previous value and surfaces the error, so the toggle never rests in
    /// a position that contradicts disk.
    func testSetUploadDefaultRevertsOnFailure() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            if args.first == "settings", args.contains("--set") {
                throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "boom"])
            }
            return self?.settingsEnvelope(privacy: nil, uploadDefault: "cloud")
        }
        let controller = PrivacyController(invoke: fake.invoker())
        await controller.refreshStatus()
        XCTAssertEqual(controller.uploadDefault, "cloud")

        let ok = await controller.setUploadDefault("ask")

        XCTAssertFalse(ok)
        XCTAssertEqual(controller.uploadDefault, "cloud", "failed write must revert the optimistic flip")
        XCTAssertEqual(controller.lastError, "boom")
    }

    /// `refreshStatus` also captures the top-level `upload_default` and
    /// `recordings_dir` (U12's toggle + storage row), even when the payload
    /// has no v2 privacy block.
    func testRefreshStatusCapturesUploadDefaultAndRecordingsDir() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            self?.settingsEnvelope(
                privacy: nil, uploadDefault: "both", recordingsDir: "/tmp/recs"
            )
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshStatus()

        XCTAssertEqual(controller.uploadDefault, "both")
        XCTAssertEqual(controller.recordingsDir, "/tmp/recs")
        XCTAssertNil(controller.status)
    }

    // MARK: - setCloudE2EE (SCR-220 U4, KTD-3)

    /// Enable/disable ship the `e2ee` verb group — never a raw
    /// `settings --set cloud_e2ee_enabled=…` — because `e2ee enable` creates
    /// the cloud key BEFORE flipping the flag (KTD-3 ordering). This argv is
    /// the contract the Python side keys on.
    func testSetCloudE2EEIssuesExpectedArgv() async {
        for (on, verb) in [(true, "enable"), (false, "disable")] {
            let fake = FakeInvoker()
            let controller = PrivacyController(invoke: fake.invoker())

            let ok = await controller.setCloudE2EE(on)

            XCTAssertTrue(ok)
            XCTAssertEqual(fake.calls, [["e2ee", verb, "--json"]])
            XCTAssertEqual(controller.cloudE2EEEnabled, on)
        }
    }

    /// A failed enable (e.g. key creation failed, R2) reverts the optimistic
    /// flip and surfaces the error — the toggle never rests ON with no key
    /// behind it, and encryption is left off rather than half-configured.
    func testSetCloudE2EERevertsOnEnableFailure() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            if args.first == "e2ee" {
                throw NSError(domain: "test", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "key creation failed"])
            }
            return self?.settingsEnvelope(privacy: nil, cloudE2EEEnabled: false)
        }
        let controller = PrivacyController(invoke: fake.invoker())
        await controller.refreshStatus()
        XCTAssertEqual(controller.cloudE2EEEnabled, false)

        let ok = await controller.setCloudE2EE(true)

        XCTAssertFalse(ok)
        XCTAssertEqual(controller.cloudE2EEEnabled, false,
                       "failed enable must revert the optimistic flip")
        XCTAssertEqual(controller.lastError, "key creation failed")
    }

    /// `refreshStatus` captures the top-level `cloud_e2ee_enabled` even when
    /// the payload has no v2 privacy block.
    func testRefreshStatusCapturesCloudE2EE() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            self?.settingsEnvelope(privacy: nil, cloudE2EEEnabled: true)
        }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshStatus()

        XCTAssertEqual(controller.cloudE2EEEnabled, true)
    }

    /// An older CLI omits `cloud_e2ee_enabled` → nil, and the policy layer
    /// renders the E2EE row locked (KTD-8 tolerance) rather than offering a
    /// toggle whose write path may not exist.
    func testRefreshStatusNilCloudE2EERendersLockedRow() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in self?.settingsEnvelope(privacy: nil) }
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.refreshStatus()

        XCTAssertNil(controller.cloudE2EEEnabled)
        XCTAssertEqual(
            PrivacySettingsPolicy.e2eeTapOutcome(
                cloudE2EEEnabled: controller.cloudE2EEEnabled,
                eligibility: .eligible
            ),
            .locked
        )
    }

    /// The R4 gate + cancel path: with the flag off, a tap resolves to the
    /// disclosure — never a direct write — and cancelling the sheet leaves
    /// the fixture untouched: the only CLI call on record is the refresh, so
    /// no `e2ee` write fires until the user confirms, and the switch state
    /// (`cloudE2EEEnabled`) is unchanged.
    func testE2EEEnableTapGatesOnDisclosureAndCancelMakesNoCLICall() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in
            self?.settingsEnvelope(privacy: nil, cloudE2EEEnabled: false)
        }
        let controller = PrivacyController(invoke: fake.invoker())
        await controller.refreshStatus()

        // The off→on tap is a disclosure, not a flip.
        XCTAssertEqual(
            PrivacySettingsPolicy.e2eeTapOutcome(
                cloudE2EEEnabled: controller.cloudE2EEEnabled,
                eligibility: .eligible
            ),
            .showDisclosure
        )
        // Cancel = the view calls nothing further. Invocation count stays at
        // the single refresh; the optimistic value never moved.
        XCTAssertEqual(fake.calls, [["settings", "--json"]])
        XCTAssertEqual(controller.cloudE2EEEnabled, false)
    }

    // MARK: - toggleAllow (U13's Record segment)

    /// Record on a matrix-masked app ships the allow_apps add vector; the app
    /// list refreshes afterward so the row converges with disk truth.
    func testToggleAllowIssuesAddArgvAndRefreshes() async {
        let fake = FakeInvoker()
        let controller = PrivacyController(invoke: fake.invoker())

        await controller.toggleAllow(bundleId: "com.tinyspeck.slackmacgap", allowed: true)

        XCTAssertEqual(fake.calls.first, [
            "settings", "privacy", "allow_apps", "add", "com.tinyspeck.slackmacgap", "--json",
        ])
        XCTAssertEqual(fake.calls.last, ["apps", "--json"])
    }

    // MARK: - startMigration (SCR-228 U6)

    /// Success flips state to `.succeeded`, sends the storage-migrate argv, and
    /// re-reads settings so the displayed recordings path updates.
    func testStartMigrationSuccessUpdatesPathAndState() async {
        let migrate = FakeInvoker()
        migrate.respond = { _ in Data(#"{"ok":true,"moved_to":"/new/recs"}"#.utf8) }
        let settings = FakeInvoker()
        settings.respond = { [weak self] args in
            guard args == ["settings", "--json"], let self else { return nil }
            return self.settingsEnvelope(privacy: nil, recordingsDir: "/new/recs")
        }
        let controller = PrivacyController(
            invoke: settings.invoker(), migrateInvoke: migrate.invoker()
        )

        await controller.startMigration(to: URL(fileURLWithPath: "/new/recs"))

        XCTAssertEqual(controller.migrationState, .succeeded(newPath: "/new/recs"))
        XCTAssertEqual(controller.recordingsDir, "/new/recs")
        XCTAssertEqual(migrate.calls.first, ["storage", "migrate", "/new/recs", "--json"])
        XCTAssertEqual(settings.calls.last, ["settings", "--json"])  // refreshStatus ran
        XCTAssertNil(controller.lastError)
    }

    /// A refusal (ok:false) carries the daemon reason code + human message into
    /// `.failed`, and the displayed path is left unchanged.
    func testStartMigrationFailureCarriesReasonAndMessage() async {
        let migrate = FakeInvoker()
        migrate.respond = { _ in
            Data(#"""
            {"ok":false,"error":"storage_migration_failed","reason":"cross_volume","message":"Pick a folder on the same disk."}
            """#.utf8)
        }
        let controller = PrivacyController(
            invoke: FakeInvoker().invoker(), migrateInvoke: migrate.invoker()
        )

        await controller.startMigration(to: URL(fileURLWithPath: "/Volumes/Ext/recs"))

        XCTAssertEqual(
            controller.migrationState,
            .failed(reason: "cross_volume", message: "Pick a folder on the same disk.")
        )
        XCTAssertNil(controller.recordingsDir)  // unchanged on refusal
    }

    /// clearMigrationState resets a terminal banner to idle so it doesn't
    /// linger across pane visits.
    func testClearMigrationStateResetsTerminalBanner() async {
        let migrate = FakeInvoker()
        migrate.respond = { _ in
            Data(#"{"ok":false,"reason":"cross_volume","message":"x"}"#.utf8)
        }
        let controller = PrivacyController(
            invoke: FakeInvoker().invoker(), migrateInvoke: migrate.invoker()
        )
        await controller.startMigration(to: URL(fileURLWithPath: "/x"))
        guard case .failed = controller.migrationState else {
            return XCTFail("expected .failed")
        }
        controller.clearMigrationState()
        XCTAssertEqual(controller.migrationState, .idle)
    }

    /// The chosen folder's filesystem path is forwarded verbatim as the target.
    func testStartMigrationForwardsTargetPath() async {
        let migrate = FakeInvoker()
        migrate.respond = { _ in Data(#"{"ok":true,"moved_to":"/Volumes/Big/recs"}"#.utf8) }
        let controller = PrivacyController(
            invoke: FakeInvoker().invoker(), migrateInvoke: migrate.invoker()
        )

        await controller.startMigration(to: URL(fileURLWithPath: "/Volumes/Big/recs"))

        XCTAssertEqual(
            migrate.calls.first, ["storage", "migrate", "/Volumes/Big/recs", "--json"]
        )
    }
}
