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

    // (The old `setProvider` seam was retired post-review — no production
    // caller remained after the pane moved to `selectLocalProvider`, whose
    // ordering/revert/argv contracts are pinned in the U2 section below.)

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
    func testSelectLocalProviderPreservesValueAsSingleArgvElement() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()
        let hostile = "on-device; rm -rf $HOME `id`"

        _ = await controller.selectLocalProvider(hostile)

        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "provider", "set", hostile, "--json"]
        ))
    }

    // MARK: - SCR-239 (U10) — endpoint + decode contracts

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

    /// R1/R2 — the user-owned matrix is 3 vendors × 2 mechanisms, and Gemini
    /// appears exactly once per mechanism (never a duplicate listing): once per
    /// mechanism in the option matrix, once on the flow's pick step, and never
    /// inside the "Included with ScreenCap" group.
    func testUserOwnedOptionsAreSixWithGeminiListedOnce() {
        let settings = decode(envelope())
        let opts = ConnectProviderModel.userOwnedOptions(settings)

        XCTAssertEqual(opts.count, 6, "3 vendors × 2 mechanisms")
        let geminiKey = opts.filter { $0.providerID == "gemini" }
        let geminiCli = opts.filter { $0.providerID == "gemini-cli" }
        XCTAssertEqual(geminiKey.count, 1)
        XCTAssertEqual(geminiCli.count, 1)
        // The add-provider flow's pick step lists Gemini exactly once.
        XCTAssertEqual(
            ConnectProviderModel.flowChoices.filter { $0.id == "gemini" }.count, 1)
        // The "Included with ScreenCap" group holds only the on-device row —
        // never any BYO vendor id (R5: no placeholder, no double-Gemini).
        let includedIDs = IntelligenceSelectionModel.groups(settings)
            .first { $0.title == IntelligenceSelectionModel.includedGroupTitle }!
            .rows.map(\.id)
        XCTAssertEqual(includedIDs, ["on-device"])
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

    // MARK: - U5 honest-copy audit (R12) + consent copy (R10)

    /// R12 — no forbidden marketing string appears anywhere in the pane's copy:
    /// no "end-to-end encryption" / "we can't see it" / "free unlimited". The
    /// corpus is the two models' `allAuditedCopy` lists — every copy static
    /// (including the U5 consent rows and trust footer) is enumerated there, so
    /// a newly added static lands in the audit or visibly next to the list it
    /// was omitted from. Enumeration completeness is spot-checked below.
    func testHonestCopyAuditNoForbiddenStrings() {
        let corpus = ConnectProviderModel.allAuditedCopy
            + IntelligenceSelectionModel.allAuditedCopy

        // Guard the guard: an emptied corpus would pass vacuously.
        XCTAssertFalse(ConnectProviderModel.allAuditedCopy.isEmpty)
        XCTAssertFalse(IntelligenceSelectionModel.allAuditedCopy.isEmpty)
        // Enumeration completeness: the U5 consent copy, the trust footer, the
        // flow copy, and the per-vendor copy are all reachable via the corpus.
        XCTAssertTrue(corpus.contains(IntelligenceSelectionModel.summaryConsentRowCaption))
        XCTAssertTrue(corpus.contains(IntelligenceSelectionModel.recallConsentRowCaption))
        XCTAssertTrue(corpus.contains(IntelligenceSelectionModel.daySplitRowCaption))
        XCTAssertTrue(corpus.contains(IntelligenceSelectionModel.framesRowCaption))
        XCTAssertTrue(corpus.contains(IntelligenceSelectionModel.consentTrustFooter))
        XCTAssertTrue(corpus.contains(ConnectProviderModel.flowPickCaption))
        XCTAssertTrue(corpus.contains(BYOVendor.gemini.cliLimitsCopy))

        let haystack = corpus.joined(separator: " ").lowercased()
        let forbidden = [
            "end-to-end encryption", "end to end encryption", "e2ee",
            "we can't see", "we cannot see", "we can't watch", "we can’t see",
            "free unlimited", "unlimited free", "unlimited usage",
        ]
        for phrase in forbidden {
            XCTAssertFalse(
                haystack.contains(phrase),
                "pane copy must not promise '\(phrase)' (R12 honesty gate)"
            )
        }
        // R12 — the removed false footer must not resurface in any copy static:
        // the consent toggles gate the cloud fallback, which can run while a
        // local row is the rendered selection.
        XCTAssertFalse(haystack.contains("only run when the active model"))
    }

    /// R12 — the Gemini free-tier rate limit is surfaced plainly in its CLI copy.
    func testGeminiCopyMentionsFreeTierLimits() {
        let copy = BYOVendor.gemini.cliLimitsCopy.lowercased()
        XCTAssertTrue(copy.contains("rate-limit") || copy.contains("requests/min")
                      || copy.contains("per day") || copy.contains("/day"),
                      "Gemini's free-tier caps must be stated plainly (R12)")
    }

    /// R1 — the two ownership group headers of the MODEL card are distinct,
    /// non-empty, and legible: whose infrastructure each group uses reads from
    /// the titles alone. (Rewritten from the retired 3-section
    /// `testHostedAndUserOwnedSectionsAreDistinct` — the hosted-section copy
    /// was retired with the reserved-slot decision, R5.)
    func testOwnershipGroupHeadersAreDistinctAndLegible() {
        let included = IntelligenceSelectionModel.includedGroupTitle
        let yourOwn = IntelligenceSelectionModel.yourOwnGroupTitle
        XCTAssertNotEqual(included, yourOwn)
        XCTAssertFalse(included.isEmpty)
        XCTAssertFalse(yourOwn.isEmpty)
        XCTAssertTrue(included.contains("ScreenCap"),
                      "the included group names whose infrastructure it is (R1)")
        XCTAssertTrue(yourOwn.lowercased().contains("your own"),
                      "the user-owned group states ownership plainly (R1)")
    }

    // MARK: - U5 consent-row copy (R10) + trust footer (R12)

    /// R10 — each of the four consent rows has a pure title/caption static (the
    /// exact strings the view renders), captioned in plain language: the two
    /// send rows state what is sent AND what never leaves; the two fixed rows
    /// state that nothing reaches a cloud model.
    func testConsentRowCopyStaticsExistPerRow() {
        typealias M = IntelligenceSelectionModel
        let rows: [(title: String, caption: String)] = [
            (M.summaryConsentRowTitle, M.summaryConsentRowCaption),
            (M.recallConsentRowTitle, M.recallConsentRowCaption),
            (M.daySplitRowTitle, M.daySplitRowCaption),
            (M.framesRowTitle, M.framesRowCaption),
        ]
        for row in rows {
            XCTAssertFalse(row.title.isEmpty)
            XCTAssertFalse(row.caption.isEmpty)
        }
        XCTAssertTrue(M.summaryConsentRowCaption.hasPrefix("Sends"))
        XCTAssertTrue(M.summaryConsentRowCaption.contains("Never screen images"))
        XCTAssertTrue(M.recallConsentRowCaption.hasPrefix("Sends"))
        XCTAssertTrue(M.recallConsentRowCaption.contains("Never screen images"))
        XCTAssertTrue(M.daySplitRowCaption.lowercased().contains("never a cloud task"))
        // Scoped to the consent-governed tasks — an unscoped "any cloud model"
        // claim is falsified by the legacy auto-namer path (follow-up).
        XCTAssertTrue(M.framesRowCaption.lowercased().contains("never send screen images"))
        XCTAssertFalse(M.framesRowCaption.lowercased().contains("any cloud model"))
    }

    /// R12 — the trust footer states the strip scope honestly, scoped to the
    /// consent-governed tasks (summaries, answers, day-splitting). It must NOT
    /// claim "any model": the legacy auto-namer path sits outside the strip
    /// (tracked as a follow-up), so the broader claim would be false. It
    /// claims nothing about uploads.
    func testTrustFooterScopesStripToConsentGovernedTasks() {
        let footer = IntelligenceSelectionModel.consentTrustFooter.lowercased()
        XCTAssertTrue(footer.contains("masked"))
        XCTAssertTrue(footer.contains("blocked"))
        XCTAssertTrue(footer.contains("summaries"))
        XCTAssertTrue(footer.contains("day-splitting"))
        XCTAssertTrue(footer.contains("local or cloud"))
        XCTAssertFalse(footer.contains("any model"),
                       "unscoped claim — falsified by the legacy auto-namer path")
        XCTAssertFalse(footer.contains("upload"),
                       "the footer is scoped to models seeing content, not uploads")
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

    // MARK: - U4 fixtures — flow settings + "Your own" rows

    private func flowSettings(
        provider: String = "on-device",
        cloudProvider: String? = nil,
        endpoint: String? = nil,
        classification: String? = nil
    ) -> IntelligenceSettings {
        IntelligenceSettings(
            provider: provider,
            cloudProvider: cloudProvider,
            summaryCloudConsent: false,
            recallCloudConsent: false,
            daySplitCloudConsent: false,
            framesCloudConsent: false,
            localServerEndpoint: endpoint,
            endpointClassification: classification
        )
    }

    private func yourOwnRows(_ settings: IntelligenceSettings) -> [IntelligenceModelRow] {
        IntelligenceSelectionModel.groups(settings)
            .first { $0.title == IntelligenceSelectionModel.yourOwnGroupTitle }!
            .rows
    }

    // MARK: - U4 — step-policy transitions (R6)

    /// "Add another provider…" starts at the pick step.
    func testFlowAddNewEntryStartsAtPick() {
        XCTAssertEqual(ConnectProviderStepPolicy.initialStep(for: .addNew), .pick)
    }

    /// A Manage/Set-up entry lands directly on configure with the row's choice
    /// preset — vendor and local-server alike.
    func testFlowManageEntryStartsAtConfigureWithPreset() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.initialStep(for: .manage(.vendor(.openai))),
            .configure(.vendor(.openai))
        )
        XCTAssertEqual(
            ConnectProviderStepPolicy.initialStep(for: .manage(.localServer)),
            .configure(.localServer)
        )
    }

    /// The pick step offers the three vendors plus Local server (R6), each
    /// advancing to its own configure step — never skipping ahead.
    func testFlowPickAdvancesToConfigureForEveryChoiceNoSkips() {
        XCTAssertEqual(
            ConnectProviderModel.flowChoices.map(\.id),
            ["openai", "anthropic", "gemini", "local-server"]
        )
        for choice in ConnectProviderModel.flowChoices {
            XCTAssertEqual(
                ConnectProviderStepPolicy.step(afterPicking: choice),
                .configure(choice)
            )
        }
        XCTAssertFalse(ConnectProviderStepPolicy.canFinish(from: .pick),
                       "done is unreachable from pick — no step skips")
    }

    /// Back from configure returns to pick ONLY for the add-new entry; a manage
    /// entry never saw a pick step, so Back is omitted there. Pick itself has
    /// no back.
    func testFlowBackFromConfigureOnlyForAddNewEntry() {
        let configure = ConnectFlowStep.configure(.vendor(.anthropic))
        XCTAssertTrue(ConnectProviderStepPolicy.canGoBack(from: configure, entry: .addNew))
        XCTAssertEqual(
            ConnectProviderStepPolicy.stepAfterBack(from: configure, entry: .addNew),
            .pick
        )
        XCTAssertFalse(ConnectProviderStepPolicy.canGoBack(
            from: configure, entry: .manage(.vendor(.anthropic))))
        XCTAssertNil(ConnectProviderStepPolicy.stepAfterBack(
            from: configure, entry: .manage(.vendor(.anthropic))))
        XCTAssertFalse(ConnectProviderStepPolicy.canGoBack(from: .pick, entry: .addNew))
    }

    /// Done is reachable only from a configure step (a completion is minted
    /// only by a terminal configure action — the sheet gates Done on holding one).
    func testFlowDoneOnlyAfterTerminalConfigureAction() {
        XCTAssertFalse(ConnectProviderStepPolicy.canFinish(from: .pick))
        XCTAssertTrue(ConnectProviderStepPolicy.canFinish(from: .configure(.localServer)))
        XCTAssertTrue(ConnectProviderStepPolicy.canFinish(from: .configure(.vendor(.gemini))))
    }

    // MARK: - U4/R8 — completion auto-selects only selectable rows

    /// A stored key's row is selectable — the completion reports it and selects
    /// it through the cloud seam (KTD2: never the active `provider`).
    func testFlowKeyCompletionSelectsKeyRow() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.completion(afterKeyStored: .anthropic),
            ConnectProviderStepPolicy.Completion(
                addedRowID: "anthropic", autoSelect: .cloudRow(id: "anthropic"))
        )
    }

    /// R8 — a CLI add selects only when the delegation CLI is available; a
    /// needs-attention CLI add finishes WITHOUT selection.
    func testFlowCLICompletionSelectsOnlyWhenAvailable() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.completion(afterCLIConfirmed: .openai, cliAvailable: true),
            ConnectProviderStepPolicy.Completion(
                addedRowID: "openai-cli", autoSelect: .cloudRow(id: "openai-cli"))
        )
        XCTAssertEqual(
            ConnectProviderStepPolicy.completion(afterCLIConfirmed: .openai, cliAvailable: false),
            ConnectProviderStepPolicy.Completion(addedRowID: "openai-cli", autoSelect: .none)
        )
    }

    // MARK: - U4/AE3 — endpoint classification, both verdicts

    /// AE3 — a LOCAL classification (`http://localhost:11434/v1`) auto-selects
    /// the local-server row via the local seam, and the row it lands on is
    /// selectable.
    func testFlowEndpointLocalClassificationSelectsSelectableRow() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.completion(afterEndpointSaved: "LOCAL"),
            ConnectProviderStepPolicy.Completion(
                addedRowID: "local-server",
                autoSelect: .localRow(provider: "local-server"))
        )
        let rows = yourOwnRows(flowSettings(
            endpoint: "http://localhost:11434/v1", classification: "LOCAL"
        ))
        XCTAssertEqual(rows.first { $0.id == "local-server" }?.selectable, true)
    }

    /// AE3/R9 — a DNS-name URL classifies REMOTE: the flow completes WITHOUT
    /// selection; the row renders present, unselectable, with the honest copy;
    /// the reported row id still drives the just-added highlight.
    func testFlowEndpointRemoteAddFinishesUnselectedHighlightedWithHonestCopy() {
        let completion = ConnectProviderStepPolicy.completion(afterEndpointSaved: "REMOTE")
        XCTAssertEqual(completion.addedRowID, "local-server")
        XCTAssertEqual(completion.autoSelect, .none)

        let settings = flowSettings(
            endpoint: "http://models.example.com/v1", classification: "REMOTE"
        )
        let server = yourOwnRows(settings).first { $0.id == "local-server" }
        XCTAssertEqual(server?.selectable, false)
        XCTAssertTrue(server?.subtitle?.contains("require a local endpoint") == true,
                      "the REMOTE row must state the consequence (AE3)")

        var highlight = JustAddedHighlight()
        highlight.flowCompleted(addedRowID: completion.addedRowID)
        XCTAssertTrue(highlight.isHighlighted("local-server"))
        XCTAssertEqual(
            IntelligenceSelectionModel.renderedSelection(settings).rowID, "on-device",
            "no auto-select happened — the selection stays where it was"
        )
    }

    /// An unreadable classification is treated as not-local — no auto-select.
    func testFlowEndpointUnclassifiedSaveFinishesWithoutSelection() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.completion(afterEndpointSaved: nil).autoSelect,
            .none
        )
    }

    // MARK: - U4/R7 — key verdicts + the stdin contract on the flow path

    /// The three key verdicts render distinct feedback copy.
    func testKeyVerdictCopyIsDistinctPerVerdict() {
        let valid = ConnectProviderModel.keyVerdictValidCopy
        let invalid = ConnectProviderModel.keyVerdictInvalidCopy(vendorName: "OpenAI")
        let unknown = ConnectProviderModel.keyVerdictUnknownCopy(vendorName: "OpenAI")
        XCTAssertEqual(Set([valid, invalid, unknown]).count, 3,
                       "valid/invalid/unknown must render distinct states")
        XCTAssertTrue(invalid.contains("Nothing was stored"),
                      "the invalid verdict states the store was blocked")
        XCTAssertTrue(unknown.lowercased().contains("verify"),
                      "the unknown verdict states the key is unverified")
    }

    /// KTD5 pinned on the flow path: across the whole terminal-action sequence
    /// (store via the stdin seam, then the completion's cloud-slot auto-select)
    /// the secret travels on STDIN and appears in no argv element anywhere.
    func testFlowKeyConnectSequencePipesSecretOverStdinNeverArgv() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()
        let secret = "sk-flow-secret-value-456"
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
        XCTAssertTrue(result.ok)

        let completion = ConnectProviderStepPolicy.completion(afterKeyStored: .openai)
        XCTAssertEqual(completion.autoSelect, .cloudRow(id: "openai"))
        _ = await controller.selectCloudProvider("openai")

        XCTAssertEqual(String(data: captured.stdin, encoding: .utf8), secret)
        XCTAssertFalse(captured.argv.contains(secret))
        XCTAssertEqual(
            captured.argv,
            ["settings", "intelligence", "--set-key", "openai", "--validate", "--json"]
        )
        XCTAssertTrue(fake.calls.contains(
            ["settings", "intelligence", "cloud_provider", "set", "openai", "--json"]
        ))
        XCTAssertFalse(fake.calls.contains { $0.contains(secret) },
                       "no argv anywhere on the flow path carries the secret")
    }

    // MARK: - U4 — disconnect: deselect-then-clear

    /// Disconnecting the currently-selected key vendor deselects FIRST
    /// (selection returns to on-device), then clears the key; a non-selected
    /// vendor — including one whose CLI id is selected — just clears.
    func testDisconnectPlanDeselectsFirstOnlyWhenVendorSelected() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.disconnectPlan(
                vendor: .openai, persistedCloudProvider: "openai"),
            [.selectOnDevice, .clearKey(vendor: "openai")]
        )
        XCTAssertEqual(
            ConnectProviderStepPolicy.disconnectPlan(
                vendor: .openai, persistedCloudProvider: nil),
            [.clearKey(vendor: "openai")]
        )
        XCTAssertEqual(
            ConnectProviderStepPolicy.disconnectPlan(
                vendor: .openai, persistedCloudProvider: "openai-cli"),
            [.clearKey(vendor: "openai")],
            "the CLI id being selected is not the key row — clearing the key must not deselect it"
        )
    }

    /// EFFECT — running the plan through the controller ships the deselect
    /// (cloud slot cleared, provider back to on-device) BEFORE `--clear-key`.
    func testDisconnectSelectedProviderIssuesDeselectThenClearArgvOrder() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        for operation in ConnectProviderStepPolicy.disconnectPlan(
            vendor: .openai, persistedCloudProvider: "openai"
        ) {
            switch operation {
            case .selectOnDevice:
                _ = await controller.selectLocalProvider("on-device")
            case .clearKey(let vendor):
                _ = await controller.clearBYOKey(vendor: vendor)
            }
        }

        let writes = fake.calls.filter { $0 != ["settings", "intelligence", "--json"] }
        XCTAssertEqual(writes, [
            ["settings", "intelligence", "cloud_provider", "set", "none", "--json"],
            ["settings", "intelligence", "provider", "set", "on-device", "--json"],
            ["settings", "intelligence", "--clear-key", "openai", "--json"],
        ])
    }

    // MARK: - U4 — endpoint writes: provider-reset-first

    /// Clearing the endpoint while `local-server` is the persisted provider
    /// resets to on-device FIRST; while it isn't, the clear stands alone.
    func testEndpointClearPlanResetsProviderFirstWhileActive() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.endpointWritePlan(
                newValue: "", persistedProvider: "local-server"),
            [.selectOnDevice, .writeEndpoint("")]
        )
        XCTAssertEqual(
            ConnectProviderStepPolicy.endpointWritePlan(
                newValue: "", persistedProvider: "on-device"),
            [.writeEndpoint("")]
        )
    }

    /// A re-save while active resets FIRST too: the LOCAL/REMOTE verdict is
    /// only known after the daemon round-trip, so a REMOTE result must never
    /// leave `provider=local-server` pointing at it even transiently (a LOCAL
    /// result re-selects via the completion's auto-select).
    func testEndpointRemoteResavePlanResetsProviderFirstWhileActive() {
        XCTAssertEqual(
            ConnectProviderStepPolicy.endpointWritePlan(
                newValue: "http://models.example.com/v1",
                persistedProvider: "local-server"),
            [.selectOnDevice, .writeEndpoint("http://models.example.com/v1")]
        )
        XCTAssertEqual(
            ConnectProviderStepPolicy.endpointWritePlan(
                newValue: "http://localhost:11434/v1",
                persistedProvider: "on-device"),
            [.writeEndpoint("http://localhost:11434/v1")]
        )
    }

    /// EFFECT — the clear-while-active plan ships the provider reset argv
    /// BEFORE the endpoint write.
    func testEndpointClearWhileActiveIssuesProviderResetBeforeEndpointWrite() async {
        let fake = FakeInvoker()
        let controller = IntelligenceController(invoke: fake.invoker())
        await controller.refresh()

        for operation in ConnectProviderStepPolicy.endpointWritePlan(
            newValue: "", persistedProvider: "local-server"
        ) {
            switch operation {
            case .selectOnDevice:
                _ = await controller.selectLocalProvider("on-device")
            case .writeEndpoint(let value):
                _ = await controller.setEndpoint(value)
            }
        }

        let writes = fake.calls.filter { $0 != ["settings", "intelligence", "--json"] }
        XCTAssertEqual(writes, [
            ["settings", "intelligence", "cloud_provider", "set", "none", "--json"],
            ["settings", "intelligence", "provider", "set", "on-device", "--json"],
            ["settings", "intelligence", "local_server_endpoint", "set", "none", "--json"],
        ])
    }

    // MARK: - U4/F2 — completion returns the row id; highlight + nudge

    /// F2/R8 — completion returns the added row id; the highlight sets from it
    /// and survives the flow's own auto-select of that row; the standing nudge
    /// fires for the newly selected cloud row exactly while its toggles are off.
    func testFlowCompletionReportsRowIDHighlightAndNudgeFollowToggles() {
        let completion = ConnectProviderStepPolicy.completion(afterKeyStored: .anthropic)
        XCTAssertEqual(completion.addedRowID, "anthropic")

        var highlight = JustAddedHighlight()
        highlight.flowCompleted(addedRowID: completion.addedRowID)
        // The auto-select lands as a selection change to the same row — the
        // chip it just set must survive.
        highlight.selectionChanged(to: completion.addedRowID)
        XCTAssertTrue(highlight.isHighlighted("anthropic"))

        XCTAssertTrue(IntelligenceSelectionModel.consentNudgeVisible(
            selectedRowID: completion.addedRowID,
            summaryCloudConsent: false, recallCloudConsent: false
        ), "off toggles → the standing nudge points at the consent section")
        XCTAssertFalse(IntelligenceSelectionModel.consentNudgeVisible(
            selectedRowID: completion.addedRowID,
            summaryCloudConsent: true, recallCloudConsent: true
        ), "no nudge when the toggles are already on")
    }

    // MARK: - U4 honest copy (KTD5 / AE3)

    /// KTD5 — the CLI configure step states plainly that no test call is made.
    func testCliConfigureCopyStatesNoTestCall() {
        XCTAssertTrue(ConnectProviderModel.cliAvailabilityHonestCopy.lowercased()
            .contains("no test call"))
    }

    /// R9/AE3 — the flow's REMOTE result copy IS the pane's non-selectable-row
    /// copy (one string, one consequence), and it states the requirement.
    func testFlowRemoteResultCopyStatesLocalEndpointRequirement() {
        XCTAssertTrue(IntelligenceSelectionModel.remoteEndpointNotSelectableCopy
            .contains("require a local endpoint"))
    }
}
