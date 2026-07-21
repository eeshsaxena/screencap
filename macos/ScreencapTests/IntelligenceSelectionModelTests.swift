import XCTest
@testable import Screencap

/// U1 (Intelligence pane redesign) — the pure MODEL-list rules, pinned before
/// any UI change: grouping/membership (R1/R5), rendered selection with the
/// legacy reconcile treatment (KTD1), write routing + the persisted-value tap
/// guard (KTD1/KTD2), REMOTE non-selectability (R9/AE3), the on-device row
/// state matrix (KTD3), and the nudge (AE1) + just-added highlight (R8)
/// predicates. Rendering is verified by build-and-run; the rules live here.
final class IntelligenceSelectionModelTests: XCTestCase {

    // MARK: - Fixture

    private func settings(
        provider: String = "on-device",
        cloudProvider: String? = nil,
        summary: Bool = false,
        recall: Bool = false,
        endpoint: String? = nil,
        classification: String? = nil,
        openaiKey: Bool = false,
        anthropicKey: Bool = false,
        geminiKey: Bool = false,
        openaiCli: Bool = false,
        anthropicCli: Bool = false,
        geminiCli: Bool = false
    ) -> IntelligenceSettings {
        IntelligenceSettings(
            provider: provider,
            cloudProvider: cloudProvider,
            summaryCloudConsent: summary,
            recallCloudConsent: recall,
            daySplitCloudConsent: false,
            framesCloudConsent: false,
            localServerEndpoint: endpoint,
            endpointClassification: classification,
            openaiKeyPresent: openaiKey,
            anthropicKeyPresent: anthropicKey,
            geminiKeyPresent: geminiKey,
            openaiCliAvailable: openaiCli,
            anthropicCliAvailable: anthropicCli,
            geminiCliAvailable: geminiCli
        )
    }

    /// The "Your own" rows for a settings read-back.
    private func yourOwnRows(_ s: IntelligenceSettings) -> [IntelligenceModelRow] {
        IntelligenceSelectionModel.groups(s)
            .first { $0.title == IntelligenceSelectionModel.yourOwnGroupTitle }!
            .rows
    }

    // MARK: - Rendered selection (KTD1 read path)

    /// `cloud_provider` set wins: the BYO row renders selected even while
    /// `provider` stays `on-device` (the two-slot state renders as one pick).
    func testCloudProviderSetSelectsBYORow() {
        let sel = IntelligenceSelectionModel.renderedSelection(
            settings(provider: "on-device", cloudProvider: "openai", openaiKey: true)
        )
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "openai", needsReconcile: false))
    }

    /// `cloud_provider` beats a valid local-server `provider` too — the cloud
    /// slot is the consented fallback answerer whenever it is set.
    func testCloudProviderBeatsLocalServerProvider() {
        let sel = IntelligenceSelectionModel.renderedSelection(
            settings(
                provider: "local-server", cloudProvider: "anthropic-cli",
                endpoint: "http://127.0.0.1:11434", classification: "LOCAL",
                anthropicCli: true
            )
        )
        XCTAssertEqual(sel.rowID, "anthropic-cli")
        XCTAssertFalse(sel.needsReconcile)
    }

    func testOnDeviceProviderSelectsOnDeviceRowPlain() {
        let sel = IntelligenceSelectionModel.renderedSelection(settings(provider: "on-device"))
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "on-device", needsReconcile: false))
    }

    /// `downloaded` is a valid local engine, not a row of its own (KTD2) — it
    /// maps to the on-device row with NO reconcile treatment.
    func testDownloadedProviderMapsToOnDeviceRowPlain() {
        let sel = IntelligenceSelectionModel.renderedSelection(settings(provider: "downloaded"))
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "on-device", needsReconcile: false))
    }

    func testLocalServerWithLocalEndpointSelectsLocalServerRow() {
        let sel = IntelligenceSelectionModel.renderedSelection(
            settings(provider: "local-server",
                     endpoint: "http://127.0.0.1:11434", classification: "LOCAL")
        )
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "local-server", needsReconcile: false))
    }

    /// `local-server` with no endpoint routes to the idle-gap heuristic on the
    /// daemon — the on-device row renders selected with the reconcile
    /// (needs-attention re-pick) treatment, never plain Ready (KTD1).
    func testLocalServerWithoutEndpointReconcilesToOnDevice() {
        let sel = IntelligenceSelectionModel.renderedSelection(
            settings(provider: "local-server", endpoint: nil, classification: nil)
        )
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "on-device", needsReconcile: true))
    }

    func testLocalServerWithRemoteEndpointReconcilesToOnDevice() {
        let sel = IntelligenceSelectionModel.renderedSelection(
            settings(provider: "local-server",
                     endpoint: "http://1.2.3.4:1234", classification: "REMOTE")
        )
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "on-device", needsReconcile: true))
    }

    /// The reconcile prompt survives a cloud selection: a legacy value stranded
    /// in the provider slot still degrades day-splitting to the heuristic, and
    /// the BYO row must not mask it (the next tap's write-through heals it).
    func testLegacyProviderSlotKeepsReconcileUnderCloudSelection() {
        let sel = IntelligenceSelectionModel.renderedSelection(
            settings(provider: "gemini", cloudProvider: "openai", openaiKey: true)
        )
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "openai", needsReconcile: true))
    }

    /// A legacy `provider=gemini` (pre-two-slot value) is mapped render-only:
    /// on-device row + reconcile treatment, no write-through migration.
    func testLegacyGeminiProviderReconcilesToOnDevice() {
        let sel = IntelligenceSelectionModel.renderedSelection(settings(provider: "gemini"))
        XCTAssertEqual(sel, RenderedModelSelection(rowID: "on-device", needsReconcile: true))
    }

    // MARK: - Grouping + membership (R1/R5/R9)

    /// R5 — "Included with Screencap" ships with exactly the on-device row and
    /// no placeholder for the reserved hosted-cloud slot.
    func testIncludedGroupIsOnDeviceOnlyNoHostedPlaceholder() {
        let groups = IntelligenceSelectionModel.groups(settings())
        XCTAssertEqual(groups.count, 2)
        XCTAssertEqual(groups[0].title, "Included with Screencap")
        XCTAssertEqual(groups[1].title, "Your own")
        XCTAssertEqual(groups[0].rows.map(\.id), ["on-device"])
        XCTAssertEqual(groups[0].rows[0].kind, .onDevice)
        XCTAssertTrue(groups[0].rows[0].selectable)
    }

    /// No keys, no CLI, no endpoint → "Your own" holds no rows (the Add
    /// affordance is the view's, not a row).
    func testYourOwnEmptyWhenNothingConnected() {
        XCTAssertTrue(yourOwnRows(settings()).isEmpty)
    }

    func testKeyPresentMaterializesRow() {
        let rows = yourOwnRows(settings(openaiKey: true))
        XCTAssertEqual(rows.map(\.id), ["openai"])
        XCTAssertEqual(rows[0].kind, .byo(providerID: "openai"))
        XCTAssertTrue(rows[0].selectable)
        XCTAssertTrue(rows[0].title.contains("OpenAI"))
    }

    /// A signed-in delegation CLI with nothing else configured materializes its
    /// row (derived membership — no explicit "add" persisted anywhere).
    func testCliAvailableMaterializesRow() {
        let rows = yourOwnRows(settings(geminiCli: true))
        XCTAssertEqual(rows.map(\.id), ["gemini-cli"])
        XCTAssertTrue(rows[0].selectable)
    }

    /// An endpoint materializes the local-server row, ordered after the BYO
    /// vendor rows; LOCAL classification makes it selectable.
    func testEndpointSetMaterializesLocalServerRowLast() {
        let rows = yourOwnRows(settings(
            endpoint: "http://127.0.0.1:11434", classification: "LOCAL", openaiKey: true
        ))
        XCTAssertEqual(rows.map(\.id), ["openai", "local-server"])
        let server = rows.last!
        XCTAssertEqual(server.kind, .localServer)
        XCTAssertTrue(server.selectable)
    }

    /// R9/AE3 — a REMOTE-classified endpoint's row renders but is not
    /// selectable, with copy stating that answers and day-splitting require a
    /// local endpoint.
    func testRemoteEndpointRowPresentButNotSelectable() {
        let rows = yourOwnRows(settings(
            endpoint: "http://1.2.3.4:1234", classification: "REMOTE"
        ))
        XCTAssertEqual(rows.map(\.id), ["local-server"])
        XCTAssertFalse(rows[0].selectable)
        XCTAssertTrue(
            rows[0].subtitle?.contains("require a local endpoint") == true,
            "REMOTE copy must state the consequence (AE3)"
        )
    }

    /// A stranded `cloud_provider` (key since cleared) keeps its row visible —
    /// needs-attention, not vanished — but unselectable.
    func testCurrentCloudProviderKeepsRowVisibleButUnselectable() {
        let rows = yourOwnRows(settings(cloudProvider: "openai", openaiKey: false))
        XCTAssertEqual(rows.map(\.id), ["openai"])
        XCTAssertFalse(rows[0].selectable)
    }

    // MARK: - Write routing (KTD1 write path / KTD2)

    /// A local pick clears the cloud slot FIRST, then sets `provider` — the
    /// order is the contract (no state may hold both slots pointing at
    /// different answerers).
    func testOnDevicePickWritesClearCloudThenOnDevice() {
        XCTAssertEqual(
            IntelligenceSelectionModel.writes(forPick: .onDevice),
            [.clearCloudProvider, .setProvider("on-device")]
        )
    }

    /// KTD2 — the on-device pick writes `on-device` ALWAYS; probe state and
    /// install state are rendering inputs only (the daemon's
    /// `ChainedOnDeviceProvider` cascades Apple → downloaded at generation
    /// time). `downloaded` must never be written — it would pin a stale probe
    /// snapshot. The routing takes no probe input by construction; this pins
    /// the value against every probe × install combination regardless.
    func testOnDevicePickAlwaysWritesOnDeviceNeverDownloaded() {
        let probes: [OnDeviceModelStatus] = [
            .available, .appleIntelligenceOff, .modelDownloading,
            .notEligible, .osUnsupported, .unknown,
        ]
        for probe in probes {
            for installed in [false, true] {
                _ = IntelligenceSelectionModel.onDeviceRowRender(
                    probe: probe, installed: installed,
                    download: .idle, daemonUnreachable: false
                )
                XCTAssertEqual(
                    IntelligenceSelectionModel.writes(forPick: .onDevice),
                    [.clearCloudProvider, .setProvider("on-device")],
                    "probe \(probe) installed=\(installed) must not change the write"
                )
            }
        }
    }

    func testLocalServerPickWritesClearCloudThenLocalServer() {
        XCTAssertEqual(
            IntelligenceSelectionModel.writes(forPick: .localServer),
            [.clearCloudProvider, .setProvider("local-server")]
        )
    }

    /// A BYO pick writes only the cloud slot — the daemon hard-rejects BYO ids
    /// on `provider`, so no `setProvider` (and no clear) may be issued.
    func testBYOPickWritesSetCloudProviderOnly() {
        XCTAssertEqual(
            IntelligenceSelectionModel.writes(forPick: .byo(providerID: "anthropic-cli")),
            [.setCloudProvider("anthropic-cli")]
        )
    }

    // MARK: - Tap no-op guard (KTD1)

    /// The guard compares against PERSISTED values, not the rendered selection:
    /// disk holds legacy `gemini`, the on-device row renders selected — tapping
    /// it must produce the writes (heal the divergence).
    func testTapOnDeviceHealsLegacyGeminiPersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .onDevice, settings: settings(provider: "gemini")
            ),
            [.clearCloudProvider, .setProvider("on-device")]
        )
    }

    /// Disk already holds what the tap would write → no-op (empty write list).
    func testTapOnDeviceNoOpWhenOnDevicePersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .onDevice, settings: settings(provider: "on-device")
            ),
            []
        )
    }

    /// `downloaded` renders as the on-device row without reconcile, but the tap
    /// still heals it to `on-device` — would-write ≠ persisted.
    func testTapOnDeviceHealsDownloadedPersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .onDevice, settings: settings(provider: "downloaded")
            ),
            [.clearCloudProvider, .setProvider("on-device")]
        )
    }

    /// A persisted cloud slot means the on-device tap is NOT a no-op even when
    /// `provider` already reads `on-device` — the clear must be issued.
    func testTapOnDeviceWritesWhenCloudProviderPersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .onDevice,
                settings: settings(provider: "on-device", cloudProvider: "openai")
            ),
            [.clearCloudProvider, .setProvider("on-device")]
        )
    }

    func testTapLocalServerNoOpWhenPersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .localServer,
                settings: settings(provider: "local-server",
                                   endpoint: "http://127.0.0.1:11434",
                                   classification: "LOCAL")
            ),
            []
        )
    }

    /// A BYO tap compares only the cloud slot it would write — whatever
    /// `provider` holds is untouched and therefore irrelevant to the guard.
    func testTapBYONoOpWhenSameCloudPersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .byo(providerID: "openai"),
                settings: settings(provider: "local-server", cloudProvider: "openai")
            ),
            []
        )
    }

    func testTapBYOWritesWhenDifferentCloudPersisted() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .byo(providerID: "openai"),
                settings: settings(provider: "on-device", cloudProvider: "gemini")
            ),
            [.setCloudProvider("openai")]
        )
    }

    /// KTD1 heal: a BYO pick over a stranded legacy provider slot fixes the
    /// slot first, then writes the cloud slot — a plain single-write pick
    /// would mask the stranded value forever (day-splitting silently on the
    /// heuristic behind a healthy-looking cloud selection).
    func testTapBYOHealsLegacyProviderSlotFirst() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .byo(providerID: "openai"),
                settings: settings(provider: "gemini")
            ),
            [.setProvider("on-device"), .setCloudProvider("openai")]
        )
    }

    /// An endpoint-less `local-server` slot is equally stranded — same heal.
    func testTapBYOHealsEndpointlessLocalServerSlotFirst() {
        XCTAssertEqual(
            IntelligenceSelectionModel.tapWrites(
                forPick: .byo(providerID: "anthropic-cli"),
                settings: settings(provider: "local-server")
            ),
            [.setProvider("on-device"), .setCloudProvider("anthropic-cli")]
        )
    }

    // MARK: - On-device row state matrix (KTD3)

    private func render(
        _ probe: OnDeviceModelStatus,
        installed: Bool = false,
        download: ModelDownloadState = .idle,
        daemonUnreachable: Bool = false
    ) -> OnDeviceRowRender {
        IntelligenceSelectionModel.onDeviceRowRender(
            probe: probe, installed: installed,
            download: download, daemonUnreachable: daemonUnreachable
        )
    }

    /// The matrix's "not ready" probes (off / ineligible / unsupported).
    private let notReadyProbes: [OnDeviceModelStatus] = [
        .appleIntelligenceOff, .notEligible, .osUnsupported,
    ]

    /// ready × any × idle — Ready (Apple Intelligence); the download affordance
    /// is demoted when not installed, absent when installed.
    func testMatrixReadyIdle() {
        let notInstalled = render(.available)
        XCTAssertEqual(notInstalled.status, .readyApple)
        XCTAssertEqual(notInstalled.affordance, .download)
        XCTAssertTrue(notInstalled.affordanceDemoted)

        let installed = render(.available, installed: true)
        XCTAssertEqual(installed.status, .readyApple)
        XCTAssertEqual(installed.affordance, .hidden)
    }

    /// SCR-274 — `upgradeOffer` is true in exactly one cell: Apple Intelligence
    /// ready, dedicated model not installed, download idle, daemon reachable (the
    /// row shows the download as a stated more-reliable upgrade). False everywhere
    /// else — installed, downloading, failed, daemon-unreachable, not-ready probes.
    func testUpgradeOfferOnlyWhenReadyAppleAndDownloadable() {
        // The one true cell (Covers AE1); `.cancelled` maps to `.idle`, so also true.
        XCTAssertTrue(render(.available).upgradeOffer)
        XCTAssertTrue(render(.available, download: .cancelled).upgradeOffer)

        // Installed → no offer (Covers AE2).
        XCTAssertFalse(render(.available, installed: true).upgradeOffer)

        // Download in flight / failed / daemon-unreachable → the download UI or a
        // disabled state is primary; no upgrade callout.
        XCTAssertFalse(render(.available, download: .downloading(bytesDone: 1, bytesTotal: 4)).upgradeOffer)
        XCTAssertFalse(render(.available, download: .failed(reason: "network error")).upgradeOffer)
        XCTAssertFalse(render(.available, daemonUnreachable: true).upgradeOffer)

        // Checking (unknown probe) and Apple's own model downloading → no offer.
        XCTAssertFalse(render(.unknown).upgradeOffer)
        XCTAssertFalse(render(.modelDownloading).upgradeOffer)

        // Not-ready probes (Covers AE3) → existing set-up path, no upgrade framing.
        for probe in notReadyProbes {
            XCTAssertFalse(render(probe).upgradeOffer, "probe \(probe)")
            XCTAssertFalse(render(probe, installed: true).upgradeOffer, "probe \(probe) installed")
        }
    }

    /// ready × not installed × downloading — Ready stays the badge; the
    /// progress + Cancel accessory rides demoted.
    func testMatrixReadyDownloadingIsDemotedProgress() {
        let r = render(.available, download: .downloading(bytesDone: 1, bytesTotal: 4))
        XCTAssertEqual(r.status, .readyApple)
        XCTAssertEqual(r.affordance, .progressCancel)
        XCTAssertTrue(r.affordanceDemoted)
    }

    /// ready × not installed × failed — Ready stays; failure reason + Retry demoted.
    func testMatrixReadyFailedIsDemotedRetry() {
        let r = render(.available, download: .failed(reason: "network error"))
        XCTAssertEqual(r.status, .readyApple)
        XCTAssertEqual(r.affordance, .retry(reason: "network error"))
        XCTAssertTrue(r.affordanceDemoted)
    }

    /// ready × not installed × cancelled — renders exactly as ready × idle.
    func testMatrixReadyCancelledEqualsIdle() {
        XCTAssertEqual(render(.available, download: .cancelled), render(.available))
    }

    /// modelDownloading × not installed × idle — info tone ("Model
    /// downloading…"), never needs-attention; download affordance demoted.
    func testMatrixAppleModelDownloadingIsInfoNotWarning() {
        let r = render(.modelDownloading)
        XCTAssertEqual(r.status, .appleModelDownloading)
        XCTAssertEqual(r.affordance, .download)
        XCTAssertTrue(r.affordanceDemoted)
    }

    /// not-ready × installed × any — Ready (downloaded model), no warning: an
    /// installed model IS a running local engine whatever the Apple probe says.
    func testMatrixNotReadyInstalledIsReadyDownloadedNoWarning() {
        for probe in notReadyProbes {
            for download in [ModelDownloadState.idle, .cancelled, .installed] {
                let r = render(probe, installed: true, download: download)
                XCTAssertEqual(r.status, .readyDownloaded, "probe \(probe), \(download)")
                XCTAssertEqual(r.affordance, .hidden)
            }
        }
    }

    /// off × not installed × idle/cancelled — needs attention WITH the System
    /// Settings deep link (the one state a system toggle fixes) + Download.
    func testMatrixOffNotInstalledNeedsAttentionWithSystemSettings() {
        for download in [ModelDownloadState.idle, .cancelled] {
            let r = render(.appleIntelligenceOff, download: download)
            XCTAssertEqual(r.status, .needsAttention(offersSystemSettings: true))
            XCTAssertEqual(r.affordance, .download)
            XCTAssertFalse(r.affordanceDemoted, "Download is the primary remedy here")
        }
    }

    /// ineligible/unsupported × not installed × idle/cancelled — needs
    /// attention with Download only (no system toggle can fix these).
    func testMatrixIneligibleUnsupportedNeedsAttentionDownloadOnly() {
        for probe in [OnDeviceModelStatus.notEligible, .osUnsupported] {
            for download in [ModelDownloadState.idle, .cancelled] {
                let r = render(probe, download: download)
                XCTAssertEqual(
                    r.status, .needsAttention(offersSystemSettings: false),
                    "probe \(probe), \(download)"
                )
                XCTAssertEqual(r.affordance, .download)
                XCTAssertFalse(r.affordanceDemoted)
            }
        }
    }

    /// any not-ready × not installed × downloading — the progress + Cancel UI
    /// is the row's primary content (no needs-attention badge competing).
    func testMatrixNotReadyDownloadingIsPromotedProgress() {
        for probe in notReadyProbes {
            let r = render(probe, download: .downloading(bytesDone: 1, bytesTotal: 2))
            XCTAssertEqual(r.status, .plain, "probe \(probe)")
            XCTAssertEqual(r.affordance, .progressCancel)
            XCTAssertFalse(r.affordanceDemoted)
        }
    }

    /// any not-ready × not installed × failed — "Download failed: reason" +
    /// Retry; insufficient disk surfaces as the carried reason (R4).
    func testMatrixNotReadyFailedCarriesReasonWithRetry() {
        for probe in notReadyProbes {
            let r = render(probe, download: .failed(reason: "insufficient disk space"))
            XCTAssertEqual(r.status, .plain, "probe \(probe)")
            XCTAssertEqual(r.affordance, .retry(reason: "insufficient disk space"))
            XCTAssertFalse(r.affordanceDemoted)
        }
    }

    /// unknown × not installed × idle — neutral checking state, no warning.
    func testMatrixUnknownIdleIsNeutralChecking() {
        let r = render(.unknown)
        XCTAssertEqual(r.status, .checking)
        XCTAssertEqual(r.affordance, .download)
        XCTAssertTrue(r.affordanceDemoted)
    }

    /// any × any × daemon unreachable — the download affordance is disabled
    /// with a start-the-daemon reason, whatever the probe and install state.
    func testMatrixDaemonUnreachableDisablesDownloadEverywhere() {
        let combos: [(OnDeviceModelStatus, Bool)] = [
            (.available, false), (.available, true),
            (.appleIntelligenceOff, false), (.notEligible, false),
            (.unknown, false), (.modelDownloading, false),
        ]
        for (probe, installed) in combos {
            let r = render(probe, installed: installed, daemonUnreachable: true)
            guard case .disabled(let reason) = r.affordance else {
                XCTFail("probe \(probe) installed=\(installed): expected disabled affordance")
                continue
            }
            XCTAssertTrue(
                reason.lowercased().contains("start the daemon"),
                "the disabled reason must tell the user to start the daemon"
            )
        }
    }

    /// A terminal `.installed` download state renders as installed even when
    /// the caller's installed flag lags the status read.
    func testMatrixInstalledDownloadStateCountsAsInstalled() {
        let r = render(.appleIntelligenceOff, installed: false, download: .installed)
        XCTAssertEqual(r.status, .readyDownloaded)
        XCTAssertEqual(r.affordance, .hidden)
    }

    // MARK: - Just-added highlight (R8)

    func testHighlightSetsOnFlowCompletion() {
        var h = JustAddedHighlight()
        XCTAssertFalse(h.isHighlighted("openai"))
        h.flowCompleted(addedRowID: "openai")
        XCTAssertTrue(h.isHighlighted("openai"))
        XCTAssertFalse(h.isHighlighted("gemini"))
    }

    /// Flow completion auto-selects the added row when selectable (R8) — that
    /// selection change must not erase the chip it just set.
    func testHighlightSurvivesAutoSelectOfAddedRow() {
        var h = JustAddedHighlight()
        h.flowCompleted(addedRowID: "openai")
        h.selectionChanged(to: "openai")
        XCTAssertTrue(h.isHighlighted("openai"))
    }

    func testHighlightClearsOnSelectionChangeToAnotherRow() {
        var h = JustAddedHighlight()
        h.flowCompleted(addedRowID: "openai")
        h.selectionChanged(to: "on-device")
        XCTAssertFalse(h.isHighlighted("openai"))
    }

    func testHighlightClearsOnPaneDisappear() {
        var h = JustAddedHighlight()
        h.flowCompleted(addedRowID: "gemini-cli")
        h.paneDisappeared()
        XCTAssertFalse(h.isHighlighted("gemini-cli"))
    }
}
