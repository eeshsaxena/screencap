import AppKit
import SwiftUI

/// The Intelligence settings pane — the two-section redesign (U3):
///
/// 1. **MODEL** — one grouped card with a single rendered selection:
///    - *Included with ScreenCap* — only the merged on-device row (R5). The row
///      absorbs the Apple-availability badge and the downloaded-model
///      download/progress/retry affordances, rendered from the pure
///      `IntelligenceSelectionModel.onDeviceRowRender` state matrix (KTD3).
///    - *Your own* — connected BYO provider rows (membership derived by
///      `IntelligenceSelectionModel.groups`) plus the local-server row when an
///      endpoint is set (selectable only when it classifies LOCAL — R9/AE3),
///      then the "Add another provider…" row opening `ConnectProviderSheet`.
///    The rendered selection comes from `renderedSelection(...)` (KTD1's read
///    path); a tap computes `tapWrites(...)` against the *persisted* slots and
///    dispatches to the controller's `selectLocalProvider`/`selectCloudProvider`
///    seams. Empty writes = no-op; re-tap deselection is removed (radio
///    semantics). A legacy persisted value renders the on-device row with the
///    reconcile treatment prompting a re-pick.
/// 2. **WHAT CLOUD MODELS MAY DO** — the per-task cloud-consent matrix (U5 —
///    all copy lives in `IntelligenceSelectionModel` statics, KTD7):
///    - Summaries & titles (R8) — a real toggle wired to `summary_cloud_consent`.
///    - Answers about your recordings — a real toggle (`recall_cloud_consent`).
///    - Splitting & labeling the day (R7) — shown as ON-DEVICE, *not* a cloud
///      toggle. It never leaves the Mac even with a cloud provider configured.
///    - Screen frames or images (R9-frames) — a FIXED "always off".
///    A standing nudge renders by the section header exactly when
///    `consentNudgeVisible(...)` says so (AE1) — it is the consent pointer.
///    The trust footer (R12) states masked/blocked apps are stripped before
///    any model — local or cloud — sees content.
///
/// Consent + provider writes flow through the CLI settings layer via
/// `IntelligenceController`; BYO keys are stored daemon-side (Keychain-class,
/// R3) and the key value never enters this process on read-back. The consent
/// toggles reuse the `AppRulesView` optimistic pending-state pattern.
struct IntelligenceSettingsView: View {
    @EnvironmentObject private var intelligence: IntelligenceController

    /// Drives the opt-in downloadable model (SCR-239 U10) — the download state
    /// machine + install state, polled from `screencap model status`. Its
    /// `daemonUnreachable` flag is the KTD3 matrix's reachability input.
    @StateObject private var download = ModelDownloadController()

    /// Locks the provider picker while a write round-trips (the AppRulesView
    /// pending pattern). Without it a rapid re-pick races the controller's
    /// in-flight guard, whose `false` return would render as a spurious error
    /// for a write that actually succeeded. (Documented double guard — the
    /// controller keeps its own quiet in-flight guard, KTD6.)
    @State private var providerWriteInFlight = false

    /// Consent rows with a write in flight (the `pendingSegments` analogue):
    /// while set, the toggle renders its optimistic position and locks so a
    /// second tap can't race the CLI round-trip. Cleared when the write settles.
    @State private var pendingConsentRows: Set<String> = []

    /// Inline error under the affected section after a failed CLI write (the
    /// optimistic flip has already been reverted by the controller).
    @State private var writeError: String?

    /// U4 — the add-provider flow presentation. nil when closed; `.addNew`
    /// opens at the pick step, `.manage(choice)` enters at configure with the
    /// row's choice preset.
    @State private var connectFlow: ConnectFlowEntry?

    /// R8 — the just-added highlight, set from the flow's completion report and
    /// cleared by the pure predicate (next selection change or pane disappear).
    @State private var justAdded = JustAddedHighlight()

    /// Live Apple-on-device availability. "On-device model" is Apple's
    /// `SystemLanguageModel`, gated by the system-wide Apple Intelligence switch —
    /// so selecting it here isn't the same as it being usable. Probed natively on
    /// appear and re-probed when the app regains focus (e.g. right after the user
    /// toggles Apple Intelligence in System Settings from the badge's link), so the
    /// on-device row shows the true state instead of looking ready-when-it-isn't.
    @State private var onDeviceStatus: OnDeviceModelStatus = .unknown

    var body: some View {
        ZStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Intelligence")
                        .font(SCTypography.paneHeading)
                        .foregroundStyle(Color.scInk)
                        .padding(.bottom, 6)
                    Text("Which model turns your recordings into named tasks, summaries, and answers.")
                        .font(SCTypography.sans(size: 13))
                        .foregroundStyle(Color.scInkMuted)
                        .padding(.bottom, 24)

                    content
                        .frame(maxWidth: 720)

                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 36)
                .padding(.vertical, 30)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Color.scCanvas)

            // U4 — the add-provider flow, presented as an in-window overlay
            // (the app's `NewRecordingSheet` pattern) rather than a native
            // `.sheet`. Completion reports the added row id for the highlight.
            if let entry = connectFlow, let settings = intelligence.settings {
                ConnectProviderSheet(
                    entry: entry,
                    settings: settings,
                    controller: intelligence,
                    onClose: { connectFlow = nil },
                    onComplete: { rowID in justAdded.flowCompleted(addedRowID: rowID) }
                )
                .transition(.opacity)
            }
        }
        .task { await intelligence.refresh() }
        .task { await download.refreshStatus() }
        .task { onDeviceStatus = OnDeviceModelStatus.probe() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            onDeviceStatus = OnDeviceModelStatus.probe()
            // KTD3 — reachability recovers on focus, like the Apple probe: a
            // transient status-read failure at pane-open must not leave the
            // download affordance disabled for the whole visit.
            Task { await download.refreshStatus() }
        }
        // R8 — the highlight's clearing edges live in the pure predicate: a
        // selection change to another row clears it (the flow's own auto-select
        // of the added row does not); leaving the pane clears it.
        .onChange(of: renderedSelectionRowID) { newRowID in
            justAdded.selectionChanged(to: newRowID)
        }
        .onDisappear { justAdded.paneDisappeared() }
    }

    /// The rendered selection's row id, for the highlight's clearing edge.
    private var renderedSelectionRowID: String? {
        intelligence.settings.map { IntelligenceSelectionModel.renderedSelection($0).rowID }
    }

    @ViewBuilder
    private var content: some View {
        if let settings = intelligence.settings {
            VStack(alignment: .leading, spacing: 28) {
                modelSection(settings)
                cloudTasksSection(settings)
                if let writeError {
                    Text("Couldn't save: \(writeError)")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scRust)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        } else if let err = intelligence.lastError {
            errorState(err)
        } else {
            loadingState
        }
    }

    // MARK: - MODEL section (grouped single-selection card, U3)

    /// One bordered card holding both ownership groups (R1). Group membership,
    /// row order, and selectability all come from the pure model — the view
    /// never re-derives them (KTD4).
    @ViewBuilder
    private func modelSection(_ settings: IntelligenceSettings) -> some View {
        let groups = IntelligenceSelectionModel.groups(settings)
        let selection = IntelligenceSelectionModel.renderedSelection(settings)
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader(IntelligenceSelectionModel.modelSectionTitle)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(groups) { group in
                    groupHeaderRow(group.title)
                    rowDivider
                    ForEach(group.rows) { row in
                        modelRow(row, settings: settings, selection: selection)
                        if row.kind == .onDevice {
                            onDeviceAccessory(settings, reconcile: selection.needsReconcile)
                        }
                        rowDivider
                    }
                }
                addProviderRow
            }
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
        }
    }

    /// An in-card ownership group header (R1) — styled like `sectionHeader`,
    /// placed between `rowDivider`s, exposed as a header to accessibility.
    private func groupHeaderRow(_ title: String) -> some View {
        Text(title)
            .font(SCTypography.metaMonoSmall)
            .tracking(1.05)
            .foregroundStyle(Color.scInkMuted)
            .accessibilityAddTraits(.isHeader)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One row of the grouped MODEL list. The on-device row is a whole-row
    /// radio button; local-server and BYO rows use the radio-as-button idiom
    /// because their trailing Manage/Set up/Connect accessories (which open the
    /// flow at its configure step, U4) stay interactive even when the radio
    /// isn't (a non-selectable REMOTE local-server row renders its radio muted
    /// and disabled — R9/AE3).
    @ViewBuilder
    private func modelRow(
        _ row: IntelligenceModelRow,
        settings: IntelligenceSettings,
        selection: RenderedModelSelection
    ) -> some View {
        let isSelected = row.id == selection.rowID
        switch row.kind {
        case .byo:
            byoModelRow(row, settings: settings, isSelected: isSelected)
        case .localServer:
            localServerModelRow(row, settings: settings, isSelected: isSelected)
        case .onDevice:
            Button {
                tapRow(row, settings: settings)
            } label: {
                HStack(alignment: .center, spacing: 12) {
                    radio(selected: isSelected, muted: !row.selectable)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(row.title)
                            .font(SCTypography.sans(size: 14, weight: .semibold))
                            .foregroundStyle(Color.scInk)
                        if let subtitle = row.subtitle {
                            Text(subtitle)
                                .font(SCTypography.sans(size: 12.5))
                                .foregroundStyle(Color.scInkMuted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 8)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(providerWriteInFlight || !row.selectable)
        }
    }

    /// The local-server row under "Your own": radio (selectable only when the
    /// endpoint classifies LOCAL — R9/AE3), title + classification subtitle,
    /// and a Manage accessory opening the flow at the local-server configure
    /// step (U4 re-homed the endpoint field there).
    private func localServerModelRow(
        _ row: IntelligenceModelRow,
        settings: IntelligenceSettings,
        isSelected: Bool
    ) -> some View {
        HStack(alignment: .center, spacing: 12) {
            selectableRadio(row, settings: settings, isSelected: isSelected)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(row.title)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    if justAdded.isHighlighted(row.id) {
                        chip(IntelligenceSelectionModel.justAddedChipLabel, color: Color.scTeal)
                    }
                }
                if let subtitle = row.subtitle {
                    Text(subtitle)
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scInkMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Spacer(minLength: 8)

            Button(IntelligenceSelectionModel.manageAccessoryTitle) {
                connectFlow = .manage(.localServer)
            }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: .semibold))
            .foregroundStyle(Color.scTeal)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .contentShape(Rectangle())
        .overlay(justAddedBorder(row.id))
    }

    /// A BYO row under "Your own": radio (when selectable), title + the existing
    /// honest billing/limits copy, and the existing needs-attention chip +
    /// Manage/Set up/Connect accessory opening `ConnectProviderSheet`.
    private func byoModelRow(
        _ row: IntelligenceModelRow,
        settings: IntelligenceSettings,
        isSelected: Bool
    ) -> some View {
        let option = ConnectProviderModel.userOwnedOptions(settings)
            .first { $0.providerID == row.id }
        return HStack(alignment: .center, spacing: 12) {
            selectableRadio(row, settings: settings, isSelected: isSelected)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(row.title)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    if justAdded.isHighlighted(row.id) {
                        chip(IntelligenceSelectionModel.justAddedChipLabel, color: Color.scTeal)
                    }
                }
                if let option {
                    Text(byoSubtitle(option))
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scInkMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Spacer(minLength: 8)

            if let option {
                byoRowAccessory(option)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .contentShape(Rectangle())
        .overlay(justAddedBorder(row.id))
    }

    /// R8 — the accent border on the just-added row, rendered alongside the
    /// chip until the predicate clears the highlight.
    @ViewBuilder
    private func justAddedBorder(_ rowID: String) -> some View {
        if justAdded.isHighlighted(rowID) {
            RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                .strokeBorder(Color.scTeal, lineWidth: 1.5)
                .padding(3)
        }
    }

    /// The "Add another provider…" row at the foot of the "Your own" group —
    /// opens the flow at its pick step (U4).
    private var addProviderRow: some View {
        Button {
            connectFlow = .addNew
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "plus.circle")
                Text(IntelligenceSelectionModel.addProviderRowTitle)
            }
            .font(SCTypography.sans(size: 13, weight: .semibold))
            .foregroundStyle(Color.scTeal)
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // KTD6 — flow-opening accessories share the radios' in-flight
        // discipline: a flow terminal action racing a pane provider write
        // would bounce off the controller's quiet guard and surface a stale
        // error for an operation that never ran.
        .disabled(providerWriteInFlight)
    }

    /// The trailing chip/button per BYO row: a "needs attention" chip for an
    /// unavailable CLI (R5/R13), a Manage button for a connected key, or a
    /// Connect button otherwise — each opening the flow at the vendor's
    /// configure step (U4). Disabled while a provider write is in flight
    /// (same rationale as the Add row).
    @ViewBuilder
    private func byoRowAccessory(_ option: BYOProviderOption) -> some View {
        switch (option.mechanism, option.state) {
        case (.cli, .needsAttention):
            HStack(spacing: 8) {
                chip("Needs attention", color: Color.scAmberText)
                Button(IntelligenceSelectionModel.setUpAccessoryTitle) {
                    connectFlow = .manage(.vendor(option.vendor))
                }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scTeal)
                .disabled(providerWriteInFlight)
            }
        case (.apiKey, .connected):
            Button(IntelligenceSelectionModel.manageAccessoryTitle) {
                connectFlow = .manage(.vendor(option.vendor))
            }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: .semibold))
            .foregroundStyle(Color.scTeal)
            .disabled(providerWriteInFlight)
        case (.apiKey, _):
            Button(IntelligenceSelectionModel.connectAccessoryTitle) {
                connectFlow = .manage(.vendor(option.vendor))
            }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: .semibold))
            .foregroundStyle(Color.scTeal)
            .disabled(providerWriteInFlight)
        default:
            EmptyView()
        }
    }

    private func byoSubtitle(_ option: BYOProviderOption) -> String {
        switch option.mechanism {
        case .apiKey:
            return option.vendor.keyBillingCopy
        case .cli:
            // Honest limits copy always; append the fix path when unavailable (R5).
            if option.state == .needsAttention {
                return option.vendor.cliLimitsCopy + " " + option.vendor.cliFixGuidance
            }
            return option.vendor.cliLimitsCopy
        }
    }

    // MARK: - Row taps (KTD1 write path)

    /// A row tap: the pure model computes the writes against the *persisted*
    /// slots (so a rendered-selected row with a legacy persisted value still
    /// heals on tap); an empty list is a no-op (re-tap deselection removed).
    /// Local picks dispatch through `selectLocalProvider` (clear-cloud-first,
    /// abort-on-failure — KTD1's ordering lives in the controller seam); BYO
    /// picks through `selectCloudProvider`.
    private func tapRow(_ row: IntelligenceModelRow, settings: IntelligenceSettings) {
        guard !providerWriteInFlight, row.selectable else { return }
        let writes = IntelligenceSelectionModel.tapWrites(
            forPick: row.kind, settings: settings
        )
        guard let terminal = writes.last else { return }
        writeError = nil
        providerWriteInFlight = true
        Task {
            let ok: Bool
            // KTD1 heal shape: a BYO pick over a stranded legacy provider slot
            // fixes the slot first, then selects the cloud row — abort the
            // cloud write when the heal fails (same policy as the local seam).
            if writes.count == 2,
               case .setProvider(let heal) = writes[0],
               case .setCloudProvider(let id) = writes[1] {
                let healed = await intelligence.selectLocalProvider(heal)
                ok = healed ? await intelligence.selectCloudProvider(id) : false
            } else {
                switch terminal {
                case .setProvider(let value):
                    ok = await intelligence.selectLocalProvider(value)
                case .setCloudProvider(let id):
                    ok = await intelligence.selectCloudProvider(id)
                case .clearCloudProvider:
                    // Never terminal in the model's write plans; nothing to do.
                    ok = true
                }
            }
            providerWriteInFlight = false
            if !ok { writeError = intelligence.lastError ?? "the provider change." }
        }
    }

    // MARK: - Merged on-device row accessories (KTD3)

    /// The availability/download accessory under the on-device row, rendered
    /// from the pure state matrix (`onDeviceRowRender`) — status line first,
    /// download affordance below, in the accessory-under-row idiom. When the
    /// rendered selection carries the reconcile flag, the needs-attention
    /// re-pick prompt replaces the status line (KTD1).
    @ViewBuilder
    private func onDeviceAccessory(_ settings: IntelligenceSettings, reconcile: Bool) -> some View {
        let render = IntelligenceSelectionModel.onDeviceRowRender(
            probe: onDeviceStatus,
            installed: settings.downloadedModelInstalled || download.isDefaultModelInstalled,
            download: download.state,
            daemonUnreachable: download.daemonUnreachable
        )
        VStack(alignment: .leading, spacing: 8) {
            if reconcile {
                statusLine(text: IntelligenceSelectionModel.reconcileNeededCopy, tone: .warn)
            } else {
                onDeviceStatusLine(render.status)
            }
            downloadAffordance(render.affordance, demoted: render.affordanceDemoted)
        }
        .padding(.horizontal, 44)
        .padding(.bottom, 12)
    }

    /// The status line for the merged row's render case. The needs-attention
    /// text reuses the probe's existing badge copy (the specific blocker), and
    /// the System Settings deep link is offered exactly when the render says a
    /// system toggle fixes it.
    @ViewBuilder
    private func onDeviceStatusLine(_ status: OnDeviceRowRender.Status) -> some View {
        switch status {
        case .readyApple:
            statusLine(text: IntelligenceSelectionModel.onDeviceReadyAppleCopy, tone: .ok)
        case .readyDownloaded:
            statusLine(text: IntelligenceSelectionModel.onDeviceReadyDownloadedCopy, tone: .ok)
        case .appleModelDownloading:
            statusLine(text: IntelligenceSelectionModel.onDeviceAppleModelDownloadingCopy, tone: .info)
        case .needsAttention(let offersSystemSettings):
            HStack(spacing: 8) {
                Circle()
                    .fill(badgeColor(.warn))
                    .frame(width: 6, height: 6)
                Text(onDeviceStatus.settingsBadge?.text
                    ?? IntelligenceSelectionModel.onDeviceUnavailableCopy)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(badgeColor(.warn))
                if offersSystemSettings {
                    Button("Turn on in System Settings") { AppleIntelligenceSettings.open() }
                        .buttonStyle(.plain)
                        .font(SCTypography.sans(size: 12, weight: .semibold))
                        .foregroundStyle(Color.scTeal)
                }
                Spacer(minLength: 0)
            }
        case .checking:
            statusLine(text: IntelligenceSelectionModel.onDeviceCheckingCopy, tone: .info)
        case .plain:
            EmptyView()
        }
    }

    /// A dot + text status line in a badge tone.
    private func statusLine(text: String, tone: OnDeviceStatusBadge.Tone) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(badgeColor(tone))
                .frame(width: 6, height: 6)
            Text(text)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(badgeColor(tone))
            Spacer(minLength: 0)
        }
    }

    /// The download affordance for the merged row's render case — Download /
    /// progress+Cancel / failed+Retry, wired to `ModelDownloadController`.
    /// Demoted (a local engine is ready or arriving) renders the Download
    /// button secondary so it never competes with a Ready badge; disabled
    /// (daemon unreachable) renders it inert with the model's reason.
    @ViewBuilder
    private func downloadAffordance(
        _ affordance: OnDeviceRowRender.DownloadAffordance, demoted: Bool
    ) -> some View {
        let sizeGB = (download.disclosedSizeBytes ?? 0) > 0
            ? ShellSidebarModel.formatStorage(download.disclosedSizeBytes!)
            : "~2 GB"
        switch affordance {
        case .hidden:
            EmptyView()
        case .download:
            if demoted {
                Button("Download (\(sizeGB))") { Task { await download.startDownload() } }
                    .buttonStyle(.plain)
                    .font(SCTypography.sans(size: 12, weight: .semibold))
                    .foregroundStyle(Color.scTeal)
            } else {
                Button("Download (\(sizeGB))") { Task { await download.startDownload() } }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            }
        case .progressCancel:
            HStack(spacing: 10) {
                ProgressView(value: download.state.fractionComplete)
                    .frame(maxWidth: 220)
                Button("Cancel") { Task { await download.cancel() } }
                    .buttonStyle(.plain).font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
                Spacer(minLength: 0)
            }
        case .retry(let reason):
            HStack(spacing: 10) {
                Text("Download failed: \(reason)")
                    .font(SCTypography.sans(size: 12)).foregroundStyle(Color.scRust)
                Button("Retry") { Task { await download.startDownload() } }
                    .font(SCTypography.sans(size: 12)).foregroundStyle(Color.scTeal)
                Spacer(minLength: 0)
            }
        case .disabled(let reason):
            HStack(spacing: 10) {
                Button("Download (\(sizeGB))") {}
                    .controlSize(.small)
                    .disabled(true)
                Text(reason)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .fixedSize(horizontal: false, vertical: true)
                // KTD3 — the in-pane recovery path: without it, one transient
                // status-read failure pins this disabled state for the visit.
                Button("Retry") { Task { await download.refreshStatus() } }
                    .buttonStyle(.plain).font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
                Spacer(minLength: 0)
            }
        }
    }

    // MARK: - WHAT CLOUD MODELS MAY DO section (consent matrix)

    /// U5 — every rendered string here is a pure copy static on
    /// `IntelligenceSelectionModel` (KTD7) so the honest-copy audit reaches it.
    private func cloudTasksSection(_ settings: IntelligenceSettings) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader(IntelligenceSelectionModel.cloudTasksSectionTitle)
            // AE1 — the standing nudge: rendered exactly when the pure predicate
            // says so (a cloud row is the rendered selection and a relevant
            // toggle is off). This IS the consent pointer — no auto-scroll.
            if IntelligenceSelectionModel.consentNudgeVisible(
                selectedRowID: IntelligenceSelectionModel.renderedSelection(settings).rowID,
                summaryCloudConsent: settings.summaryCloudConsent,
                recallCloudConsent: settings.recallCloudConsent
            ) {
                Text(IntelligenceSelectionModel.consentNudgeCopy)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scAmberText)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 2)
                    .padding(.bottom, 2)
            }
            VStack(alignment: .leading, spacing: 0) {
                // R8 — summaries/titles: a real cloud toggle.
                consentToggleRow(
                    title: IntelligenceSelectionModel.summaryConsentRowTitle,
                    subtitle: IntelligenceSelectionModel.summaryConsentRowCaption,
                    row: "summary_cloud_consent",
                    on: settings.summaryCloudConsent
                )
                rowDivider
                // R10 — recall answers: also a real cloud toggle (Chat depends
                // on it — the design's three rows predate Chat).
                consentToggleRow(
                    title: IntelligenceSelectionModel.recallConsentRowTitle,
                    subtitle: IntelligenceSelectionModel.recallConsentRowCaption,
                    row: "recall_cloud_consent",
                    on: settings.recallCloudConsent
                )
                rowDivider
                // R7 — day-splitting/labeling: NOT a cloud toggle. Stays on the
                // Mac even with a cloud provider configured; shown as a fixed
                // "On-device" chip, no toggle.
                fixedRow(
                    title: IntelligenceSelectionModel.daySplitRowTitle,
                    subtitle: IntelligenceSelectionModel.daySplitRowCaption,
                    badge: (IntelligenceSelectionModel.daySplitChipLabel, Color.scTeal)
                )
                rowDivider
                // R9 — frames/images: fixed "always off", non-interactive.
                fixedRow(
                    title: IntelligenceSelectionModel.framesRowTitle,
                    subtitle: IntelligenceSelectionModel.framesRowCaption,
                    badge: (IntelligenceSelectionModel.framesChipLabel, Color.scInkMuted)
                )
            }
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
            // R12 — the trust footer. (The earlier "Cloud tasks only run when
            // the active model above is a cloud provider" line was removed as
            // false: the consent toggles gate the cloud *fallback*, which can
            // run while a local row is the rendered selection.)
            Text(IntelligenceSelectionModel.consentTrustFooter)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 2)
        }
    }

    /// A settable cloud-consent row (R8/R10) — a real toggle with the
    /// optimistic pending-state discipline.
    private func consentToggleRow(title: String, subtitle: String, row: String, on: Bool) -> some View {
        let pending = pendingConsentRows.contains(row)
        return HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(subtitle)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            SettingsToggle(on: on) { toggleConsent(row: row, currentlyOn: on) }
                .disabled(pending)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 16)
    }

    private func toggleConsent(row: String, currentlyOn: Bool) {
        guard !pendingConsentRows.contains(row) else { return }
        writeError = nil
        pendingConsentRows.insert(row)
        Task {
            let ok = await intelligence.setConsent(row: row, enabled: !currentlyOn)
            pendingConsentRows.remove(row)
            if !ok { writeError = intelligence.lastError ?? "the consent change." }
        }
    }

    /// A fixed, non-interactive row (R7 day-split / R9 frames) — a state badge,
    /// no toggle. It displays a rule the user cannot change.
    private func fixedRow(title: String, subtitle: String, badge: (label: String, color: Color)) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(subtitle)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            chip(badge.label, color: badge.color)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 16)
        .opacity(0.9)
    }

    // MARK: - States

    private var loadingState: some View {
        HStack(spacing: 12) {
            ProgressView().controlSize(.small)
            Text("Loading intelligence settings…")
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInkMuted)
        }
        .padding(.vertical, 20)
    }

    private func errorState(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Couldn't load intelligence settings")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(message)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)
            Button("Retry") { Task { await intelligence.refresh() } }
                .buttonStyle(.borderedProminent)
        }
        .padding(.vertical, 20)
    }

    // MARK: - Shared chrome

    private var rowDivider: some View {
        Rectangle().fill(Color.scFillSubtle).frame(height: 1)
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(SCTypography.metaMonoSmall)
            .tracking(1.05)
            .foregroundStyle(Color.scInkMuted)
            .accessibilityAddTraits(.isHeader)
    }

    /// Map an on-device status-badge tone to its themed color.
    private func badgeColor(_ tone: OnDeviceStatusBadge.Tone) -> Color {
        switch tone {
        case .ok: return Color.scSuccessFg
        case .warn: return Color.scAmberText
        case .info: return Color.scInkMuted
        }
    }

    /// The leading radio for a "Your own" row (BYO + local-server share it): a
    /// tappable radio when the row is selectable, else a muted, non-interactive
    /// one so a stranded (selected-but-unavailable) row still renders its
    /// selection honestly.
    @ViewBuilder
    private func selectableRadio(
        _ row: IntelligenceModelRow,
        settings: IntelligenceSettings,
        isSelected: Bool
    ) -> some View {
        if row.selectable {
            Button { tapRow(row, settings: settings) } label: {
                radio(selected: isSelected, muted: false)
            }
            .buttonStyle(.plain)
            .disabled(providerWriteInFlight)
        } else {
            // A disabled control, not a bare shape (the pane's existing
            // idiom), so VoiceOver exposes the not-selectable state; the
            // radio glyph itself stays accessibility-hidden.
            Button {} label: {
                radio(selected: isSelected, muted: true)
            }
            .buttonStyle(.plain)
            .disabled(true)
            .accessibilityLabel(
                row.kind == .localServer
                    ? IntelligenceSelectionModel.remoteEndpointNotSelectableCopy
                    : "Not selectable"
            )
        }
    }

    /// The provider picker's radio dot — teal filled when selected, hollow
    /// otherwise (muted for non-selectable rows).
    private func radio(selected: Bool, muted: Bool) -> some View {
        Circle()
            .strokeBorder(
                selected ? Color.scTeal : Color.scInkFaint.opacity(muted ? 0.4 : 0.7),
                lineWidth: 1.5
            )
            .frame(width: 16, height: 16)
            .overlay {
                if selected {
                    Circle().fill(Color.scTeal).frame(width: 8, height: 8)
                }
            }
            .accessibilityHidden(true)
    }

    private func chip(_ label: String, color: Color) -> some View {
        Text(label)
            .font(SCTypography.mono(size: 9.5))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .overlay(Capsule().strokeBorder(color.opacity(0.35), lineWidth: 1))
    }
}

