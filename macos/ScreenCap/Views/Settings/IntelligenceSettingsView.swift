import SwiftUI

/// U9 — the Intelligence settings pane (R2, R6, R7, R8, R9). Two sections
/// following the `PrivacySettingsView` layout:
///
/// 1. **MODEL** — a picker for the active provider: "On-device model" (the
///    zero-config default, R3), a configured cloud provider ("Claude — your API
///    key" in the design; the CLI's only cloud id today is `gemini`), and an
///    "Add another provider…" affordance (a stub until the add-key flow lands —
///    keys are daemon-owned, so there is no in-app key entry here).
/// 2. **WHAT CLOUD MODELS MAY DO** — the per-task cloud-consent matrix:
///    - Summaries & titles (R8) — a real toggle wired to `summary_cloud_consent`.
///    - Splitting & labeling the day (R7) — shown as ON-DEVICE, *not* a cloud
///      toggle. It never leaves the Mac even with a cloud provider configured.
///    - Screen frames or images (R9) — a FIXED "always off", non-interactive.
///
/// Recall-answering (`recall_cloud_consent`) is surfaced as a fourth cloud row
/// so the pane matches the CLI's settable matrix; the design's three named rows
/// map to summary/day-split/frames.
///
/// All writes flow through the U8 CLI settings layer via `IntelligenceController`
/// (R8 — no Keychain; cloud keys stay daemon-owned). The consent toggles reuse
/// the `AppRulesView` optimistic pending-state pattern.
struct IntelligenceSettingsView: View {
    @EnvironmentObject private var intelligence: IntelligenceController

    /// Drives the opt-in downloadable model (SCR-239 U10) — the download state
    /// machine + install state, polled from `screencap model status`.
    @StateObject private var download = ModelDownloadController()

    /// The endpoint field draft for the Local-server row (committed on Save).
    @State private var endpointDraft: String = ""

    /// Locks the provider picker while a write round-trips (the AppRulesView
    /// pending pattern). Without it a rapid re-pick races the controller's
    /// in-flight guard, whose `false` return would render as a spurious error
    /// for a write that actually succeeded.
    @State private var providerWriteInFlight = false

    /// Consent rows with a write in flight (the `pendingSegments` analogue):
    /// while set, the toggle renders its optimistic position and locks so a
    /// second tap can't race the CLI round-trip. Cleared when the write settles.
    @State private var pendingConsentRows: Set<String> = []

    /// Inline error under the affected section after a failed CLI write (the
    /// optimistic flip has already been reverted by the controller).
    @State private var writeError: String?

    var body: some View {
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
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color.scCanvas)
        .task { await intelligence.refresh() }
        .task { await download.refreshStatus() }
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

    // MARK: - MODEL section (provider picker)

    private func modelSection(_ settings: IntelligenceSettings) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("MODEL")
            VStack(alignment: .leading, spacing: 0) {
                let opts = IntelligenceProviderOption.options(
                    cloudProvider: settings.cloudProvider,
                    downloadedInstalled: settings.downloadedModelInstalled
                        || download.isDefaultModelInstalled,
                    localEndpoint: settings.localServerEndpoint,
                    endpointClassification: settings.endpointClassification
                )
                ForEach(opts) { option in
                    providerRow(option, settings: settings)
                    if option.id == "downloaded" {
                        downloadAccessory(settings)
                    }
                    if option.id == "local-server" {
                        endpointField(settings)
                    }
                    if option.id != IntelligenceProviderOption.addProviderID {
                        rowDivider
                    }
                }
            }
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
        }
    }

    private func providerRow(_ option: IntelligenceProviderOption, settings: IntelligenceSettings) -> some View {
        let isSelected = option.providerValue == settings.provider
        let isAddRow = option.id == IntelligenceProviderOption.addProviderID
        return Button {
            selectProvider(option)
        } label: {
            HStack(alignment: .center, spacing: 12) {
                radio(selected: isSelected, muted: isAddRow)
                VStack(alignment: .leading, spacing: 3) {
                    Text(option.title)
                        .font(SCTypography.sans(size: 14, weight: isAddRow ? .regular : .semibold))
                        .foregroundStyle(isAddRow ? Color.scTeal : Color.scInk)
                    if let subtitle = option.subtitle {
                        Text(subtitle)
                            .font(SCTypography.sans(size: 12.5))
                            .foregroundStyle(Color.scInkMuted)
                    }
                }
                Spacer(minLength: 8)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // The "add provider" affordance is a stub; the downloaded model can't be
        // selected until it's installed (the Download button below is the CTA).
        .disabled(providerWriteInFlight || isAddRow || downloadedNotInstalled(option, settings))
        .help(isAddRow ? "Coming soon — add a cloud provider with your own API key" : "")
    }

    /// The downloaded-model radio is inert until the model is on disk.
    private func downloadedNotInstalled(_ option: IntelligenceProviderOption, _ settings: IntelligenceSettings) -> Bool {
        option.id == "downloaded"
            && !(settings.downloadedModelInstalled || download.isDefaultModelInstalled)
    }

    private func selectProvider(_ option: IntelligenceProviderOption) {
        guard !providerWriteInFlight, let value = option.providerValue else { return }
        guard value != intelligence.settings?.provider else { return }
        writeError = nil
        providerWriteInFlight = true
        Task {
            let ok = await intelligence.setProvider(value)
            providerWriteInFlight = false
            if !ok { writeError = intelligence.lastError ?? "the provider change." }
        }
    }

    // MARK: - SCR-239 Downloaded-model + Local-server accessories

    /// The Download button / progress / failed-Retry affordance for the
    /// Downloaded-model row (KTD11 progress; failed surfaces the backend cause).
    @ViewBuilder
    private func downloadAccessory(_ settings: IntelligenceSettings) -> some View {
        let sizeGB = (download.disclosedSizeBytes ?? 0) > 0
            ? ShellSidebarModel.formatStorage(download.disclosedSizeBytes!)
            : "~2 GB"
        HStack(spacing: 10) {
            switch download.state {
            case .installed:
                EmptyView()
            case let .downloading(done, total):
                ProgressView(value: total > 0 ? Double(done) / Double(total) : nil)
                    .frame(maxWidth: 220)
                Button("Cancel") { Task { await download.cancel() } }
                    .buttonStyle(.plain).font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
            case let .failed(reason):
                Text("Download failed: \(reason)")
                    .font(SCTypography.sans(size: 12)).foregroundStyle(Color.scRust)
                Button("Retry") { Task { await download.startDownload() } }
                    .font(SCTypography.sans(size: 12)).foregroundStyle(Color.scTeal)
            case .idle, .cancelled:
                if !(settings.downloadedModelInstalled || download.isDefaultModelInstalled) {
                    Button("Download (\(sizeGB))") { Task { await download.startDownload() } }
                        .buttonStyle(.borderedProminent).controlSize(.small)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 44)
        .padding(.bottom, download.state == .installed ? 0 : 12)
    }

    /// The endpoint text field for the Local-server row (U9 write via the CLI),
    /// showing the resolved LOCAL/REMOTE classification and its treatment.
    private func endpointField(_ settings: IntelligenceSettings) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                TextField("http://127.0.0.1:11434", text: $endpointDraft)
                    .textFieldStyle(.roundedBorder)
                    .font(SCTypography.mono(size: 12))
                    .frame(maxWidth: 320)
                Button("Save") {
                    Task {
                        _ = await intelligence.setEndpoint(endpointDraft)
                        endpointDraft = intelligence.settings?.localServerEndpoint ?? endpointDraft
                    }
                }
                .controlSize(.small)
                .disabled(endpointDraft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            if let cls = settings.endpointClassification {
                Text(cls == "LOCAL"
                    ? "Local — day-splitting runs against this server; nothing leaves the Mac."
                    : "Remote — treated as a cloud provider (consent-gated; day-splitting stays off).")
                    .font(SCTypography.sans(size: 11.5))
                    .foregroundStyle(cls == "LOCAL" ? Color.scTeal : Color.scInkMuted)
            }
        }
        .padding(.horizontal, 44)
        .padding(.bottom, 12)
        .onAppear { endpointDraft = settings.localServerEndpoint ?? "" }
    }

    // MARK: - WHAT CLOUD MODELS MAY DO section (consent matrix)

    private func cloudTasksSection(_ settings: IntelligenceSettings) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("WHAT CLOUD MODELS MAY DO")
            VStack(alignment: .leading, spacing: 0) {
                // R8 — summaries/titles: the one real cloud toggle.
                consentToggleRow(
                    title: "Summaries & titles",
                    subtitle: "Sends transcript text — never screen images.",
                    row: "summary_cloud_consent",
                    on: settings.summaryCloudConsent
                )
                rowDivider
                // R10 — recall answers: also a real cloud toggle once a provider
                // is configured. (The design's three rows omit it; the CLI
                // exposes it, so it's surfaced here for parity.)
                consentToggleRow(
                    title: "Answering Recall searches",
                    subtitle: "Sends transcript text — never screen images.",
                    row: "recall_cloud_consent",
                    on: settings.recallCloudConsent
                )
                rowDivider
                // R7 — day-splitting/labeling: NOT a cloud toggle. Stays on the
                // Mac even with a cloud provider configured; shown as a fixed
                // "On-device" chip, no toggle.
                fixedRow(
                    title: "Splitting & labeling the day",
                    subtitle: "Always runs on this Mac — never sent to a cloud model.",
                    badge: ("On-device", Color.scTeal)
                )
                rowDivider
                // R9 — frames/images: fixed "always off", non-interactive.
                fixedRow(
                    title: "Screen frames or images",
                    subtitle: "Never sent to any cloud model. This isn't a setting.",
                    badge: ("Always off", Color.scInkMuted)
                )
            }
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
            Text("Cloud tasks only run when the active model above is a cloud provider.")
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkMuted)
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

    /// The provider picker's radio dot — teal filled when selected, hollow
    /// otherwise (muted for the non-selectable "add" row).
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

/// A pure model of the provider-picker rows (R2) — factored out so the "which
/// options render, and which CLI provider value each writes" rules are unit
/// testable without a running view.
struct IntelligenceProviderOption: Identifiable, Equatable {
    /// Sentinel id for the non-selectable "add another provider" affordance.
    static let addProviderID = "add"

    let id: String
    let title: String
    let subtitle: String?
    /// The `provider set <value>` argument this row writes, or nil for the
    /// non-selectable "add" row.
    let providerValue: String?

    /// Build the picker rows. Always leads with the on-device default (R3), then
    /// the SCR-239 opt-in **Downloaded model** and **Local server** rows, then a
    /// configured cloud provider when set, then the "add another provider…"
    /// affordance. Subtitles reflect the downloaded-model install state and the
    /// BYO endpoint's LOCAL/REMOTE classification.
    static func options(
        cloudProvider: String?,
        downloadedInstalled: Bool = false,
        localEndpoint: String? = nil,
        endpointClassification: String? = nil
    ) -> [IntelligenceProviderOption] {
        var opts: [IntelligenceProviderOption] = [
            IntelligenceProviderOption(
                id: "on-device",
                title: "On-device model",
                subtitle: "Built into macOS · runs on this Mac · nothing leaves",
                providerValue: "on-device"
            ),
            IntelligenceProviderOption(
                id: "downloaded",
                title: "Downloaded model",
                subtitle: downloadedInstalled
                    ? "Downloaded · runs on this Mac · nothing leaves"
                    : "Download to get named tasks on any Mac (~2 GB, opt-in)",
                providerValue: "downloaded"
            ),
            IntelligenceProviderOption(
                id: "local-server",
                title: "Local server (bring your own)",
                subtitle: localServerSubtitle(localEndpoint, endpointClassification),
                providerValue: "local-server"
            ),
        ]
        if let cloud = cloudProvider {
            opts.append(IntelligenceProviderOption(
                id: cloud,
                title: displayName(for: cloud),
                subtitle: "Cloud model · your API key",
                providerValue: cloud
            ))
        }
        opts.append(IntelligenceProviderOption(
            id: addProviderID,
            title: "Add another provider…",
            subtitle: nil,
            providerValue: nil
        ))
        return opts
    }

    /// Subtitle for the Local-server row: the redacted endpoint + its treatment,
    /// or a prompt to configure one.
    static func localServerSubtitle(_ endpoint: String?, _ classification: String?) -> String? {
        guard let endpoint, !endpoint.isEmpty else {
            return "Ollama / LM Studio on this Mac — set an endpoint below"
        }
        return classification == "LOCAL"
            ? "\(endpoint) · on-device · day-splitting on"
            : "\(endpoint) · remote · treated as cloud (day-split off)"
    }

    /// A human label for a cloud provider id.
    static func displayName(for provider: String) -> String {
        switch provider {
        case "gemini": return "Gemini — your API key"
        default: return "\(provider.capitalized) — your API key"
        }
    }
}
