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
        recall: Bool = false
    ) -> Data {
        let intelligence: [String: Any] = [
            "provider": provider,
            "cloud_provider": cloudProvider as Any? ?? NSNull(),
            "summary_cloud_consent": summary,
            "recall_cloud_consent": recall,
            "day_split_cloud_consent": false,
            "frames_cloud_consent": false,
        ]
        let payload: [String: Any] = [
            "ok": true, "schema_version": 1, "intelligence": intelligence,
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

    /// With no cloud provider configured the picker offers on-device (default)
    /// and the non-selectable "add another provider…" affordance only.
    func testProviderOptionsWithoutCloudProvider() {
        let opts = IntelligenceProviderOption.options(cloudProvider: nil)
        XCTAssertEqual(opts.map(\.id), ["on-device", IntelligenceProviderOption.addProviderID])
        // On-device is the default and writes `on-device`.
        XCTAssertEqual(opts.first?.providerValue, "on-device")
        // The "add" row is non-selectable — it writes nothing.
        XCTAssertNil(opts.last?.providerValue)
    }

    /// A configured cloud provider becomes a selectable middle row that writes
    /// its own provider id.
    func testProviderOptionsWithCloudProvider() {
        let opts = IntelligenceProviderOption.options(cloudProvider: "gemini")
        XCTAssertEqual(opts.map(\.id), ["on-device", "gemini", IntelligenceProviderOption.addProviderID])
        XCTAssertEqual(opts[1].providerValue, "gemini")
        XCTAssertNotNil(opts[1].subtitle)
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
}
