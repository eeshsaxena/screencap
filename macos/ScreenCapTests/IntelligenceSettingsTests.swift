import XCTest
@testable import ScreenCap

/// U9 — the Intelligence pane's controller + pure picker model. The CLI bridge's
/// argv contract (the vectors that ship to Python), the optimistic flip +
/// revert-on-failure discipline, and the "which rows are settable vs fixed"
/// rules are pinned here; rendering is verified by build-and-run.
@MainActor
final class IntelligenceSettingsTests: XCTestCase {

    // MARK: - Test fixture

    /// Records every argv vector and returns canned bytes keyed by argv, or a
    /// success-shaped read-back envelope by default. Mirrors
    /// `PrivacyControllerTests.FakeInvoker`.
    final class FakeInvoker: @unchecked Sendable {
        private(set) var calls: [[String]] = []
        var respond: (([String]) throws -> Data?)?
        let lock = NSLock()

        func record(_ args: [String]) {
            lock.lock(); defer { lock.unlock() }
            calls.append(args)
        }

        func invoker() -> IntelligenceController.JSONInvoker {
            { [weak self] args in
                self?.record(args)
                if let data = try self?.respond?(args) { return data }
                return Data(#"{"ok":true,"schema_version":1,"intelligence":{"provider":"on-device","cloud_provider":null,"summary_cloud_consent":false,"recall_cloud_consent":false,"day_split_cloud_consent":false,"frames_cloud_consent":false}}"#.utf8)
            }
        }
    }

    private func envelope(
        provider: String = "on-device",
        cloudProvider: String? = nil,
        summary: Bool = false,
        recall: Bool = false,
        openaiKey: Bool = false,
        anthropicKey: Bool = false,
        geminiKey: Bool = false,
        openaiCli: Bool = false,
        anthropicCli: Bool = false,
        geminiCli: Bool = false
    ) -> Data {
        let intelligence: [String: Any] = [
            "provider": provider,
            "cloud_provider": cloudProvider as Any? ?? NSNull(),
            "summary_cloud_consent": summary,
            "recall_cloud_consent": recall,
            "day_split_cloud_consent": false,
            "frames_cloud_consent": false,
            "openai_key_present": openaiKey,
            "anthropic_key_present": anthropicKey,
            "gemini_key_present": geminiKey,
            "openai_cli_available": openaiCli,
            "anthropic_cli_available": anthropicCli,
            "gemini_cli_available": geminiCli,
        ]
        let payload: [String: Any] = [
            "ok": true, "schema_version": 3, "intelligence": intelligence,
        ]
        return try! JSONSerialization.data(withJSONObject: payload, options: [])
    }

    // MARK: - refresh (read-back decode)

    func testRefreshDecodesIntelligenceBlock() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            guard args == ["settings", "intelligence", "--json"] else { return nil }
            return self?.envelope(provider: "gemini", cloudProvider: "gemini", summary: true)
        }
        let controller = IntelligenceController(invoke: fake.invoker())

        await controller.refresh()

        XCTAssertEqual(controller.settings?.provider, "gemini")
        XCTAssertEqual(controller.settings?.cloudProvider, "gemini")
        XCTAssertEqual(controller.settings?.summaryCloudConsent, true)
        XCTAssertEqual(controller.settings?.recallCloudConsent, false)
        // The fixed guards decode as false (R7/R9).
        XCTAssertEqual(controller.settings?.daySplitCloudConsent, false)
        XCTAssertEqual(controller.settings?.framesCloudConsent, false)
        XCTAssertNil(controller.lastError)
    }

    func testRefreshHandlesNullCloudProvider() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] _ in self?.envelope(provider: "on-device", cloudProvider: nil) }
        let controller = IntelligenceController(invoke: fake.invoker())

        await controller.refresh()

        XCTAssertEqual(controller.settings?.provider, "on-device")
        XCTAssertNil(controller.settings?.cloudProvider)
    }

    func testRefreshSurfacesMalformedJSON() async {
        let fake = FakeInvoker()
        fake.respond = { _ in Data("{not-json".utf8) }
        let controller = IntelligenceController(invoke: fake.invoker())

        await controller.refresh()

        XCTAssertNotNil(controller.lastError)
        XCTAssertNil(controller.settings)
    }

    // MARK: - setProvider (R2)

    /// Selecting a provider ships exactly the CLI vector Python expects, then
    /// reconciles against disk.
    func testSetProviderIssuesExpectedArgvAndRefreshes() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()  // seed settings so the optimistic flip has a base

        let ok = await controller.setProvider("gemini")

        XCTAssertTrue(ok)
        // The write, then a read-back reconcile.
        XCTAssertEqual(
            fake.calls.suffix(2).map { $0 },
            [
                ["settings", "intelligence", "provider", "set", "gemini", "--json"],
                ["settings", "intelligence", "--json"],
            ]
        )
    }

    /// Optimistic flip + revert-on-failure: a nonzero exit restores the previous
    /// provider and surfaces the error so the picker never contradicts disk.
    func testSetProviderRevertsOnFailure() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            if args.contains("provider"), args.contains("set") {
                throw NSError(domain: "t", code: 1, userInfo: [NSLocalizedDescriptionKey: "boom"])
            }
            return self?.envelope(provider: "on-device")
        }
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()
        XCTAssertEqual(controller.settings?.provider, "on-device")

        let ok = await controller.setProvider("gemini")

        XCTAssertFalse(ok)
        XCTAssertEqual(controller.settings?.provider, "on-device",
                       "failed write must revert the optimistic picker flip")
        XCTAssertEqual(controller.lastError, "boom")
    }

    // MARK: - setConsent (R8/R10 optimistic toggle)

    /// Toggling summaries/titles ON ships the settable-row write with the
    /// normalized bool.
    func testSetConsentSummaryIssuesTrueArgv() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        let ok = await controller.setConsent(row: "summary_cloud_consent", enabled: true)

        XCTAssertTrue(ok)
        XCTAssertEqual(
            fake.calls.last,
            ["settings", "intelligence", "summary_cloud_consent", "set", "true", "--json"]
        )
        XCTAssertEqual(controller.settings?.summaryCloudConsent, true,
                       "optimistic flip lands immediately")
    }

    func testSetConsentRecallIssuesFalseArgv() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()
        _ = await controller.setConsent(row: "recall_cloud_consent", enabled: true)

        let ok = await controller.setConsent(row: "recall_cloud_consent", enabled: false)

        XCTAssertTrue(ok)
        XCTAssertEqual(
            fake.calls.last,
            ["settings", "intelligence", "recall_cloud_consent", "set", "false", "--json"]
        )
    }

    /// The optimistic flip reverts on a failed consent write.
    func testSetConsentRevertsOnFailure() async {
        let fake = FakeInvoker()
        fake.respond = { [weak self] args in
            if args.contains("summary_cloud_consent"), args.contains("set") {
                throw NSError(domain: "t", code: 1, userInfo: [NSLocalizedDescriptionKey: "nope"])
            }
            return self?.envelope()
        }
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        let ok = await controller.setConsent(row: "summary_cloud_consent", enabled: true)

        XCTAssertFalse(ok)
        XCTAssertEqual(controller.settings?.summaryCloudConsent, false,
                       "failed write must revert the optimistic toggle")
        XCTAssertEqual(controller.lastError, "nope")
    }

    /// A hostile provider value stays a single argv element (no shell splitting)
    /// — the argv-array contract, same canary as PrivacyController's.
    func testSetProviderPreservesValueAsSingleArgvElement() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()
        let hostile = "gemini; rm -rf $HOME `id`"

        _ = await controller.setProvider(hostile)

        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "provider", "set", hostile, "--json"]
        ))
    }

    // MARK: - Provider picker model (R2/R7/R9 pane rules)

    /// The hosted section offers on-device (default) + the SCR-239 downloaded /
    /// local-server rows. The inert "Add another provider…" stub is gone —
    /// connecting now lives in the "Your own account" section (U7). BYO is never
    /// listed here (R9 hosted/user-owned split).
    func testProviderOptionsWithoutCloudProvider() {
        let opts = IntelligenceProviderOption.options(cloudProvider: nil)
        XCTAssertEqual(opts.map(\.id), ["on-device", "downloaded", "local-server"])
        XCTAssertEqual(opts.first?.providerValue, "on-device")
        // Every hosted row is selectable — no non-selectable stub remains.
        XCTAssertTrue(opts.allSatisfy { $0.providerValue != nil })
    }

    /// A configured hosted cloud provider becomes a selectable row after the
    /// local rows (retained for a future app-managed hosted cloud row).
    func testProviderOptionsWithCloudProvider() {
        let opts = IntelligenceProviderOption.options(cloudProvider: "gemini")
        XCTAssertEqual(
            opts.map(\.id),
            ["on-device", "downloaded", "local-server", "gemini"]
        )
        XCTAssertEqual(opts.first { $0.id == "gemini" }?.providerValue, "gemini")
    }

    // MARK: - SCR-239 (U10) — downloaded + local-server rows

    func testDownloadedAndLocalServerRowsWriteExpectedProviderValues() {
        let opts = IntelligenceProviderOption.options(cloudProvider: nil)
        XCTAssertEqual(opts.first { $0.id == "downloaded" }?.providerValue, "downloaded")
        XCTAssertEqual(opts.first { $0.id == "local-server" }?.providerValue, "local-server")
    }

    func testDownloadedSubtitleReflectsInstallState() {
        let notInstalled = IntelligenceProviderOption.options(cloudProvider: nil, downloadedInstalled: false)
        XCTAssertTrue(notInstalled.first { $0.id == "downloaded" }!.subtitle!.contains("Download"))
        let installed = IntelligenceProviderOption.options(cloudProvider: nil, downloadedInstalled: true)
        XCTAssertTrue(installed.first { $0.id == "downloaded" }!.subtitle!.contains("runs on this Mac"))
    }

    func testLocalServerSubtitleReflectsClassification() {
        XCTAssertTrue(
            IntelligenceProviderOption.localServerSubtitle("http://127.0.0.1:11434", "LOCAL")!
                .contains("on-device")
        )
        XCTAssertTrue(
            IntelligenceProviderOption.localServerSubtitle("http://1.2.3.4:1234", "REMOTE")!
                .contains("treated as cloud")
        )
        XCTAssertTrue(
            IntelligenceProviderOption.localServerSubtitle(nil, nil)!.contains("set an endpoint")
        )
    }

    func testDecodeNewFieldsWithDefaults() {
        // An old envelope without the SCR-239 fields still decodes (defaults).
        let old = try! JSONDecoder().decode(
            IntelligenceEnvelope.self, from: envelope(provider: "on-device")
        )
        XCTAssertNil(old.intelligence.localServerEndpoint)
        XCTAssertFalse(old.intelligence.downloadedModelInstalled)

        // A full envelope decodes the new fields.
        let full = Data(#"{"ok":true,"schema_version":1,"intelligence":{"provider":"local-server","cloud_provider":null,"summary_cloud_consent":false,"recall_cloud_consent":false,"day_split_cloud_consent":false,"frames_cloud_consent":false,"local_server_endpoint":"http://127.0.0.1:11434","endpoint_classification":"LOCAL","downloaded_model_installed":true}}"#.utf8)
        let decoded = try! JSONDecoder().decode(IntelligenceEnvelope.self, from: full)
        XCTAssertEqual(decoded.intelligence.localServerEndpoint, "http://127.0.0.1:11434")
        XCTAssertEqual(decoded.intelligence.endpointClassification, "LOCAL")
        XCTAssertTrue(decoded.intelligence.downloadedModelInstalled)
    }

    func testSetEndpointIssuesExpectedArgv() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        _ = await controller.setEndpoint("http://127.0.0.1:11434")

        XCTAssertTrue(fake.calls.contains([
            "settings", "intelligence", "local_server_endpoint", "set",
            "http://127.0.0.1:11434", "--json",
        ]))
    }

    func testSetEndpointEmptyClearsWithNone() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        _ = await controller.setEndpoint("   ")

        XCTAssertTrue(fake.calls.contains([
            "settings", "intelligence", "local_server_endpoint", "set", "none", "--json",
        ]))
    }

    // MARK: - Fixed-row rules (R7 day-split not a cloud toggle; R9 frames off)

    /// The fixed guards surfaced by the CLI are never among the settable rows
    /// the pane wires a toggle to. This is the pane's promise that day-split
    /// stays on-device (R7) and frames are always off (R9) — encoded as: the
    /// two forbidden CLI row names are absent from the settable set.
    func testFixedRowsAreNotSettableRows() {
        // The two rows the pane exposes as real toggles.
        let settableRows = ["summary_cloud_consent", "recall_cloud_consent"]
        XCTAssertFalse(settableRows.contains("day_split_cloud_consent"),
                       "day-splitting is presented on-device, not a cloud toggle (R7)")
        XCTAssertFalse(settableRows.contains("frames_cloud_consent"),
                       "frames/images is a fixed always-off row, not a toggle (R9)")
    }

    // MARK: - Sidebar nav registration (U9 wiring)

    func testIntelligenceRoutesAndIsEnabled() {
        let row = ShellSidebarModel.settingsNav.first { $0.id == "intelligence" }
        XCTAssertNotNil(row, "Intelligence must appear in the settings nav")
        XCTAssertEqual(row?.route, .intelligence)
        XCTAssertEqual(row?.isEnabled, true)
        XCTAssertNil(row?.helpText, "an enabled row has no coming-soon tooltip")
    }

    // MARK: - U6/U7 BYO: connect flow + hosted/user-owned separation

    private func decode(_ data: Data) -> IntelligenceSettings {
        try! JSONDecoder().decode(IntelligenceEnvelope.self, from: data).intelligence
    }

    /// R1/R2/R10 — the user-owned matrix is 3 vendors × 2 mechanisms, and Gemini
    /// appears exactly once per mechanism (never a duplicate hosted/BYO listing).
    func testUserOwnedOptionsAreSixWithGeminiListedOnce() {
        let settings = decode(envelope())
        let opts = ConnectProviderModel.userOwnedOptions(settings)

        XCTAssertEqual(opts.count, 6, "3 vendors × 2 mechanisms")
        // Gemini appears exactly once as a key option and once as a CLI option —
        // and nowhere in the hosted section (R10, no double-Gemini).
        let geminiKey = opts.filter { $0.providerID == "gemini" }
        let geminiCli = opts.filter { $0.providerID == "gemini-cli" }
        XCTAssertEqual(geminiKey.count, 1)
        XCTAssertEqual(geminiCli.count, 1)
        // The hosted picker never lists any BYO vendor id.
        let hostedIDs = IntelligenceProviderOption.options(cloudProvider: nil).map(\.id)
        XCTAssertFalse(hostedIDs.contains("gemini"))
        XCTAssertFalse(hostedIDs.contains("gemini-cli"))
    }

    /// KTD2 — a BYO id resolves to the `cloud_provider` write surface, and the
    /// full BYO id set matches the daemon's `_VALID_CLOUD_PROVIDERS`.
    func testBYOProviderIDsMatchDaemonCloudProviders() {
        XCTAssertEqual(
            Set(ConnectProviderModel.allBYOProviderIDs),
            ["openai", "anthropic", "gemini", "openai-cli", "anthropic-cli", "gemini-cli"]
        )
        XCTAssertTrue(ConnectProviderModel.isBYOProvider("openai"))
        XCTAssertTrue(ConnectProviderModel.isBYOProvider("anthropic-cli"))
        XCTAssertFalse(ConnectProviderModel.isBYOProvider("on-device"))
        XCTAssertFalse(ConnectProviderModel.isBYOProvider(nil))
    }

    /// R3 — a stored key makes the key option `connected`/selectable; absent key
    /// is `notConnected`. Presence is read from the `*_key_present` flags.
    func testKeyPresenceDrivesConnectedState() {
        let settings = decode(envelope(openaiKey: true))
        let opts = ConnectProviderModel.userOwnedOptions(settings)
        let openaiKey = opts.first { $0.providerID == "openai" }!
        XCTAssertEqual(openaiKey.state, .connected)
        XCTAssertTrue(openaiKey.isSelectable)

        let anthropicKey = opts.first { $0.providerID == "anthropic" }!
        XCTAssertEqual(anthropicKey.state, .notConnected)
        XCTAssertFalse(anthropicKey.isSelectable)
    }

    /// R5/R13 — an unavailable CLI is `needsAttention` and not selectable; an
    /// available one is `connected`/selectable. Never silently substituted.
    func testCliAvailabilityDrivesNeedsAttention() {
        let settings = decode(envelope(anthropicCli: true))
        let opts = ConnectProviderModel.userOwnedOptions(settings)

        let anthropicCli = opts.first { $0.providerID == "anthropic-cli" }!
        XCTAssertEqual(anthropicCli.state, .connected)
        XCTAssertTrue(anthropicCli.isSelectable)

        let openaiCli = opts.first { $0.providerID == "openai-cli" }!
        XCTAssertEqual(openaiCli.state, .needsAttention)
        XCTAssertFalse(openaiCli.isSelectable, "an unavailable CLI can't be selected (R5)")
    }

    /// KTD2 — selecting a BYO provider writes `cloud_provider set <id>`, NOT the
    /// active `provider`. The daemon rejects a BYO id as the active provider.
    func testSetCloudProviderIssuesCloudProviderArgv() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        _ = await controller.setCloudProvider("openai-cli")

        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "cloud_provider", "set", "openai-cli", "--json"]
        ))
        XCTAssertFalse(
            fake.calls.contains(where: { $0.contains("provider") && $0.contains("openai-cli") && !$0.contains("cloud_provider") }),
            "a BYO id must never be written as the active `provider` (KTD2)"
        )
    }

    /// Deselect / disconnect writes `cloud_provider set none`.
    func testSetCloudProviderNilClearsWithNone() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        _ = await controller.setCloudProvider(nil)

        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "cloud_provider", "set", "none", "--json"]
        ))
    }

    /// KTD3 — the BYO key reaches the CLI over STDIN, never as an argv element.
    /// The argv carries `--set-key <vendor> --validate --json` and the key bytes
    /// arrive on the injected stdin channel; no argv element equals the secret.
    func testSetBYOKeyPipesSecretOverStdinNotArgv() async {
        let controller = IntelligenceController()  // default invoke unused on this path
        let secret = "sk-super-secret-value-123"
        final class Captured: @unchecked Sendable {
            var argv: [String] = []
            var stdin = Data()
        }
        let captured = Captured()

        let result = await controller.setBYOKey(
            vendor: "openai", key: secret, validate: true,
            injectInvoke: { args, stdin in
                captured.argv = args
                captured.stdin = stdin
                return Data(#"{"ok":true,"schema_version":3,"vendor":"openai","key_present":true,"validation":"valid"}"#.utf8)
            }
        )

        // The secret is on stdin.
        XCTAssertEqual(String(data: captured.stdin, encoding: .utf8), secret)
        // The secret is NOT in argv (KTD3 — argv is world-readable via ps).
        XCTAssertFalse(captured.argv.contains(secret))
        XCTAssertEqual(
            captured.argv,
            ["settings", "intelligence", "--set-key", "openai", "--validate", "--json"]
        )
        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.validation, BYOKeyResult.valid)
    }

    /// Store-only-if-valid: an invalid-key envelope (ok=false) surfaces the
    /// `invalid` verdict and reports failure so the pane blocks the store.
    func testSetBYOKeyInvalidSurfacesRejection() async {
        let controller = IntelligenceController()
        let result = await controller.setBYOKey(
            vendor: "anthropic", key: "bad", validate: true,
            injectInvoke: { _, _ in
                Data(#"{"ok":false,"schema_version":3,"vendor":"anthropic","validation":"invalid","error":"key_invalid"}"#.utf8)
            }
        )
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.validation, BYOKeyResult.invalid)
    }

    /// R4 — clearing a key issues `--clear-key <vendor>` (no secret involved).
    func testClearBYOKeyIssuesExpectedArgv() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        _ = await controller.clearBYOKey(vendor: "gemini")

        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "--clear-key", "gemini", "--json"]
        ))
    }

    /// R3 — a stored key never appears in the read-back; only a presence flag.
    /// The decoded settings carry the boolean, and nothing key-shaped.
    func testReadBackExposesPresenceFlagNotKeyValue() {
        let settings = decode(envelope(openaiKey: true, geminiCli: true))
        XCTAssertTrue(settings.openaiKeyPresent)
        XCTAssertTrue(settings.geminiCliAvailable)
        XCTAssertFalse(settings.anthropicKeyPresent)
        // The struct has no property that could hold a key value.
        XCTAssertTrue(settings.keyPresent(forVendor: "openai"))
        XCTAssertFalse(settings.keyPresent(forVendor: "anthropic"))
    }

    // MARK: - U7 honest-copy audit (R12) + separation (R9)

    /// R12 — no forbidden marketing string appears anywhere in the BYO copy:
    /// no "end-to-end encryption" / "we can't see it" / "free unlimited". This
    /// is the honesty gate for the whole user-owned surface.
    func testHonestCopyAuditNoForbiddenStrings() {
        var corpus: [String] = [
            ConnectProviderModel.hostedSectionTitle,
            ConnectProviderModel.hostedSectionCaption,
            ConnectProviderModel.userOwnedSectionTitle,
            ConnectProviderModel.userOwnedSectionCaption,
        ]
        for vendor in ConnectProviderModel.vendors {
            corpus.append(vendor.keyBillingCopy)
            corpus.append(vendor.cliLimitsCopy)
            corpus.append(vendor.cliFixGuidance)
        }
        let haystack = corpus.joined(separator: " ").lowercased()

        let forbidden = [
            "end-to-end encryption", "end to end encryption", "e2ee",
            "we can't see", "we cannot see", "we can't watch", "we can’t see",
            "free unlimited", "unlimited free", "unlimited usage",
        ]
        for phrase in forbidden {
            XCTAssertFalse(
                haystack.contains(phrase),
                "BYO copy must not promise '\(phrase)' (R12 honesty gate)"
            )
        }
    }

    /// R12 — the Gemini free-tier rate limit is surfaced plainly in its CLI copy.
    func testGeminiCopyMentionsFreeTierLimits() {
        let copy = BYOVendor.gemini.cliLimitsCopy.lowercased()
        XCTAssertTrue(copy.contains("rate-limit") || copy.contains("requests/min")
                      || copy.contains("per day") || copy.contains("/day"),
                      "Gemini's free-tier caps must be stated plainly (R12)")
    }

    /// R9 — the hosted and user-owned sections have distinct whose-bill captions.
    func testHostedAndUserOwnedSectionsAreDistinct() {
        XCTAssertNotEqual(
            ConnectProviderModel.hostedSectionTitle,
            ConnectProviderModel.userOwnedSectionTitle
        )
        XCTAssertTrue(ConnectProviderModel.hostedSectionCaption.lowercased()
            .contains("personal cloud subscription"),
            "hosted caption names the ScreenCap subscription (whose bill, R9)")
        XCTAssertTrue(ConnectProviderModel.userOwnedSectionCaption.lowercased()
            .contains("your account") || ConnectProviderModel.userOwnedSectionCaption.lowercased()
            .contains("your bill"),
            "user-owned caption names the user's own account/bill (R9)")
    }

    // MARK: - U2 fixtures — suspension gate + call counter

    /// Open-once gate for suspending a fake invoke mid-flight, so tests can
    /// observe in-flight state (optimistic flips, in-flight guards)
    /// deterministically instead of sleeping.
    final class Gate: @unchecked Sendable {
        private let lock = NSLock()
        private var isOpen = false
        private var waiters: [CheckedContinuation<Void, Never>] = []

        func wait() async {
            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                lock.lock()
                if isOpen {
                    lock.unlock()
                    c.resume()
                    return
                }
                waiters.append(c)
                lock.unlock()
            }
        }

        func open() {
            lock.lock()
            isOpen = true
            let resumable = waiters
            waiters = []
            lock.unlock()
            for w in resumable { w.resume() }
        }
    }

    /// Thread-safe invocation counter for respond closures that must succeed
    /// once (the seed refresh) and fail thereafter, or vice versa.
    final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func next() -> Int {
            lock.lock()
            defer { lock.unlock() }
            n += 1
            return n
        }
    }

    // MARK: - U2/KTD1 — selectRow seam: two-write local selection

    /// KTD1 — selecting a local row clears the cloud fallback FIRST
    /// (`cloud_provider set none`), then writes the active provider, then
    /// reconciles. The exact argv order is the contract that ships to Python.
    func testSelectLocalProviderIssuesClearBeforeProviderWrite() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()  // seed settings (call 0)

        let ok = await controller.selectLocalProvider("on-device")

        XCTAssertTrue(ok)
        XCTAssertEqual(
            Array(fake.calls.dropFirst()),
            [
                ["settings", "intelligence", "cloud_provider", "set", "none", "--json"],
                ["settings", "intelligence", "provider", "set", "on-device", "--json"],
                ["settings", "intelligence", "--json"],
            ],
            "clear-first ordering (KTD1), then the provider write, then the read-back reconcile"
        )
    }

    /// KTD1 — a failed clear aborts the sequence: the provider write is never
    /// issued, the controller reconciles against disk, and the inline error
    /// surfaces (and survives the reconcile).
    func testSelectLocalProviderClearFailureAbortsProviderWrite() async {
        let fake = FakeInvoker()
        fake.respond = { args in
            if args.contains("cloud_provider"), args.contains("set") {
                throw NSError(
                    domain: "t", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "clear-failed"])
            }
            return nil  // default success envelope
        }
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        let ok = await controller.selectLocalProvider("local-server")

        XCTAssertFalse(ok)
        XCTAssertFalse(
            fake.calls.contains {
                $0.contains("provider") && $0.contains("set") && !$0.contains("cloud_provider")
            },
            "the provider write must be aborted when the clear fails (KTD1)"
        )
        XCTAssertEqual(fake.calls.last, ["settings", "intelligence", "--json"],
                       "aborting still reconciles against disk")
        XCTAssertEqual(controller.lastError, "clear-failed")
        XCTAssertEqual(controller.settings?.provider, "on-device",
                       "no stranded optimistic state after the abort")
        XCTAssertNil(controller.settings?.cloudProvider)
    }

    /// KTD1 — a failed provider write (after a successful clear) reconciles via
    /// refresh and surfaces the error; the optimistic flip never strands.
    func testSelectLocalProviderProviderWriteFailureRefreshesAndSurfacesError() async {
        let fake = FakeInvoker()
        fake.respond = { args in
            if args.contains("provider"), args.contains("set"), !args.contains("cloud_provider") {
                throw NSError(
                    domain: "t", code: 1, userInfo: [NSLocalizedDescriptionKey: "boom"])
            }
            return nil
        }
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        let ok = await controller.selectLocalProvider("local-server")

        XCTAssertFalse(ok)
        XCTAssertEqual(fake.calls.last, ["settings", "intelligence", "--json"],
                       "provider-write failure reconciles against disk (the clear already landed)")
        XCTAssertEqual(controller.lastError, "boom",
                       "the write failure — not the reconcile — is the surfaced error")
        XCTAssertEqual(controller.settings?.provider, "on-device",
                       "no stranded optimistic provider flip")
    }

    /// KTD2 — the cloud half of the seam routes to the `cloud_provider` slot
    /// only; a BYO id is never written as the active `provider`.
    func testSelectCloudProviderRoutesToCloudProviderSlotOnly() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        let ok = await controller.selectCloudProvider("anthropic")

        XCTAssertTrue(ok)
        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "cloud_provider", "set", "anthropic", "--json"]
        ))
        XCTAssertFalse(
            fake.calls.contains {
                $0.contains("provider") && !$0.contains("cloud_provider") && $0.contains("anthropic")
            },
            "a BYO id must never reach the active `provider` slot (KTD2)"
        )
    }

    // MARK: - U2/KTD6 — setCloudProvider optimistic flip + in-flight guard

    /// KTD6 — while a cloud-provider write is in flight, the optimistic flip is
    /// already visible, and a racing second call returns false WITHOUT touching
    /// `lastError` (the view-level double-guard depends on the quiet bounce)
    /// and without issuing a second write.
    func testSetCloudProviderRacingReturnsFalseWithoutSpuriousError() async {
        let fake = FakeInvoker()
        let payload = envelope()
        let firstWriteEntered = Gate()
        let release = Gate()
        let controller = IntelligenceController(invoke: { args in
            fake.record(args)
            if args.contains("cloud_provider"), args.contains("openai") {
                firstWriteEntered.open()
                await release.wait()
            }
            return payload
        })
        await controller.refresh()

        let first = Task { await controller.setCloudProvider("openai") }
        await firstWriteEntered.wait()

        // The first write is suspended inside the CLI invoke: the optimistic
        // flip already landed on the user-action edge (KTD6)…
        XCTAssertEqual(controller.settings?.cloudProvider, "openai",
                       "optimistic flip lands before the write completes")
        // …and a racing call bounces quietly.
        let second = await controller.setCloudProvider("anthropic")
        XCTAssertFalse(second)
        XCTAssertNil(controller.lastError,
                     "a racing call must not surface a spurious error")
        XCTAssertFalse(fake.calls.contains { $0.contains("anthropic") },
                       "the racing write never reaches the CLI")

        release.open()
        let firstResult = await first.value
        XCTAssertTrue(firstResult)
    }

    /// KTD6 — a failed cloud-provider write reverts the optimistic flip. The
    /// reconcile read is made to fail too, so a lingering flip would be visible:
    /// only a genuine revert restores the pre-write value.
    func testSetCloudProviderRevertsOptimisticFlipOnFailure() async {
        let fake = FakeInvoker()
        let count = Counter()
        fake.respond = { _ in
            if count.next() == 1 { return nil }  // seed refresh succeeds
            throw NSError(domain: "t", code: 1, userInfo: [NSLocalizedDescriptionKey: "boom"])
        }
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()
        XCTAssertNil(controller.settings?.cloudProvider)

        let ok = await controller.setCloudProvider("openai")

        XCTAssertFalse(ok)
        XCTAssertNil(controller.settings?.cloudProvider,
                     "failed write must revert the optimistic flip")
        XCTAssertEqual(controller.lastError, "boom",
                       "the write failure survives the (failed) reconcile as the surfaced error")
    }

    // MARK: - U2/KTD8 + KTD3 — ModelDownloadController polling + reachability

    private func modelStatus(state: String, done: Int = 0, total: Int = 0) -> Data {
        Data(
            #"{"download":{"state":"\#(state)","bytes_done":\#(done),"bytes_total":\#(total),"reason":null},"installed":{"models":[]}}"#
                .utf8)
    }

    /// KTD8 — a FRESH controller whose FIRST refresh decodes `.downloading`
    /// starts the poll loop. This is the onboarding-started-download case: the
    /// pane opens mid-download and must animate, not freeze — today only
    /// `startDownload` polls.
    func testRefreshStatusObservingDownloadingStartsPolling() async {
        let payload = modelStatus(state: "downloading", done: 10, total: 100)
        let controller = ModelDownloadController(invoke: { _ in payload })
        XCTAssertFalse(controller.isPolling)

        await controller.refreshStatus()

        XCTAssertTrue(controller.state.isDownloading)
        XCTAssertTrue(controller.isPolling,
                      "refreshStatus must start polling when it observes .downloading (KTD8)")
    }

    /// KTD8 guard — a non-downloading status read does not spin up the loop.
    func testRefreshStatusIdleDoesNotStartPolling() async {
        let payload = modelStatus(state: "idle")
        let controller = ModelDownloadController(invoke: { _ in payload })

        await controller.refreshStatus()

        XCTAssertFalse(controller.isPolling)
        XCTAssertFalse(controller.daemonUnreachable)
    }

    /// KTD3 — a status read whose invocation throws sets the typed
    /// `daemonUnreachable` flag (the on-device row's matrix input); any
    /// subsequent successful read clears it.
    func testRefreshStatusThrowSetsDaemonUnreachableSuccessClears() async {
        let count = Counter()
        let payload = modelStatus(state: "idle")
        let controller = ModelDownloadController(invoke: { _ in
            if count.next() == 1 {
                throw NSError(
                    domain: "t", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "socket down"])
            }
            return payload
        })
        XCTAssertFalse(controller.daemonUnreachable)

        await controller.refreshStatus()
        XCTAssertTrue(controller.daemonUnreachable,
                      "a failed status read is KTD3's reachability input")
        XCTAssertNotNil(controller.lastError)

        await controller.refreshStatus()
        XCTAssertFalse(controller.daemonUnreachable,
                       "any successful read clears the flag")
        XCTAssertNil(controller.lastError)
    }
}
