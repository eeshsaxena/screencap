import SwiftUI

/// U6 — the connect-a-provider flow (R1-R5, R13). An in-window overlay (the
/// app's `NewRecordingSheet` pattern, KTD-4) rather than a native `.sheet`:
///
/// 1. **Choose a vendor** — OpenAI / Anthropic / Gemini (R1).
/// 2. **Choose a mechanism** — paste an API key, or delegate to the signed-in
///    provider CLI (R2).
///    - **API key**: a `SecureField`. The entered secret is handed to
///      `IntelligenceController.setBYOKey`, which pipes it to the daemon over
///      STDIN of `settings intelligence --set-key <vendor> --validate` — NEVER
///      an argv element (KTD3). Validation feedback (valid / invalid / unknown)
///      and store-only-if-valid come straight from the daemon.
///    - **CLI delegation**: shows the vendor's `*-cli` availability from the
///      read-back; when unavailable, renders disabled with install/sign-in
///      guidance (R5) and reuses the same surface for a runtime needs-attention
///      state (R13).
///
/// Selecting a connected provider persists it as the consented `cloud_provider`
/// (KTD2) — the sheet's Connect action does the key store + `cloud_provider set`;
/// Disconnect clears the key and/or deselects. Copy is honest (R12): no
/// "free unlimited" / "we can't see it" / E2EE strings.
struct ConnectProviderSheet: View {
    let initialVendor: BYOVendor?
    let settings: IntelligenceSettings
    let controller: IntelligenceController
    let onClose: () -> Void

    @State private var vendor: BYOVendor
    @State private var mechanism: BYOMechanism = .apiKey
    @State private var keyDraft: String = ""
    @State private var busy = false
    /// The last write's result surfaced inline (valid/invalid/unknown/error).
    @State private var feedback: Feedback?

    init(
        initialVendor: BYOVendor?,
        settings: IntelligenceSettings,
        controller: IntelligenceController,
        onClose: @escaping () -> Void
    ) {
        self.initialVendor = initialVendor
        self.settings = settings
        self.controller = controller
        self.onClose = onClose
        _vendor = State(initialValue: initialVendor ?? .openai)
        // If the pre-selected vendor's CLI is the trouble, open on the CLI tab.
        if let v = initialVendor, !settings.cliAvailable(forProviderID: v.cliProviderID),
           !settings.keyPresent(forVendor: v.rawValue) {
            _mechanism = State(initialValue: .cli)
        }
    }

    enum Feedback: Equatable {
        case valid
        case invalid
        case unknown
        case error(String)
        case cleared
    }

    var body: some View {
        ZStack(alignment: .top) {
            scrim
            panel
                .frame(width: 480)
                .padding(.top, 60)
        }
    }

    private var scrim: some View {
        Color.scDarkCanvas.opacity(0.35)
            .ignoresSafeArea()
            .contentShape(Rectangle())
            .onTapGesture { if !busy { onClose() } }
            .accessibilityHidden(true)
    }

    private var panel: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().overlay(Color.scBorderWarm)
            VStack(alignment: .leading, spacing: 20) {
                vendorPicker
                mechanismPicker
                mechanismBody
                if let feedback { feedbackRow(feedback) }
            }
            .padding(20)
        }
        .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusCard))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusCard)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .shadow(color: Color.scDarkCanvas.opacity(0.35), radius: 30, y: 24)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Text("Connect a provider")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Spacer(minLength: 8)
            Button {
                onClose()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.scInkMuted)
            }
            .buttonStyle(.plain)
            .disabled(busy)
        }
        .padding(16)
    }

    // MARK: - Vendor + mechanism pickers

    private var vendorPicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            fieldLabel("PROVIDER")
            Picker("", selection: $vendor) {
                ForEach(ConnectProviderModel.vendors) { v in
                    Text(v.displayName).tag(v)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .onChange(of: vendor) { _ in
                keyDraft = ""
                feedback = nil
            }
        }
    }

    private var mechanismPicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            fieldLabel("HOW TO CONNECT")
            Picker("", selection: $mechanism) {
                ForEach(BYOMechanism.allCases) { m in
                    Text(m.label).tag(m)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            .onChange(of: mechanism) { _ in feedback = nil }
        }
    }

    @ViewBuilder
    private var mechanismBody: some View {
        switch mechanism {
        case .apiKey: apiKeyBody
        case .cli: cliBody
        }
    }

    // MARK: - API-key mechanism (R3)

    private var apiKeyBody: some View {
        let connected = settings.keyPresent(forVendor: vendor.rawValue)
        return VStack(alignment: .leading, spacing: 10) {
            Text(vendor.keyBillingCopy)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)

            // SecureField — the key is never rendered and never becomes an argv
            // element; `setBYOKey` pipes it over STDIN (KTD3).
            SecureField(connected ? "Enter a new key to replace the stored one" : "Paste your \(vendor.displayName) API key", text: $keyDraft)
                .textFieldStyle(.roundedBorder)
                .font(SCTypography.mono(size: 12))
                .disabled(busy)

            HStack(spacing: 10) {
                Button(connected ? "Replace & connect" : "Connect") {
                    Task { await connectKey() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(busy || keyDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                if connected {
                    Button("Disconnect") {
                        Task { await disconnectKey() }
                    }
                    .controlSize(.small)
                    .disabled(busy)
                }

                if busy { ProgressView().controlSize(.small) }
                Spacer(minLength: 0)
            }

            if connected {
                Label("Connected — stored securely on this Mac", systemImage: "checkmark.seal")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
            }
        }
    }

    // MARK: - CLI-delegation mechanism (R4/R5/R13)

    private var cliBody: some View {
        let available = settings.cliAvailable(forProviderID: vendor.cliProviderID)
        return VStack(alignment: .leading, spacing: 10) {
            Text(vendor.cliLimitsCopy)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)

            if available {
                Label("`\(vendor.cliBinaryName)` is installed and signed in",
                      systemImage: "checkmark.seal")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
                Button("Use my subscription") {
                    Task { await selectCLI() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(busy)
            } else {
                // R5 / R13 — unavailable surface: needs-attention chip + the fix
                // path. Never silently substitutes another provider.
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.scAmberText)
                    Text("`\(vendor.cliBinaryName)` isn't available")
                        .font(SCTypography.sans(size: 13, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                }
                Text(vendor.cliFixGuidance)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Use my subscription") {}
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(true)
            }
            if busy { ProgressView().controlSize(.small) }
        }
    }

    // MARK: - Feedback

    @ViewBuilder
    private func feedbackRow(_ feedback: Feedback) -> some View {
        switch feedback {
        case .valid:
            feedbackLabel("Key verified and connected.", color: Color.scTeal, icon: "checkmark.circle.fill")
        case .invalid:
            feedbackLabel("That key was rejected by \(vendor.displayName). Nothing was stored.",
                          color: Color.scRust, icon: "xmark.octagon.fill")
        case .unknown:
            feedbackLabel("Stored, but couldn't reach \(vendor.displayName) to verify it. It'll be used as-is.",
                          color: Color.scAmberText, icon: "questionmark.circle.fill")
        case let .error(msg):
            feedbackLabel(msg, color: Color.scRust, icon: "exclamationmark.triangle.fill")
        case .cleared:
            feedbackLabel("Disconnected.", color: Color.scInkMuted, icon: "minus.circle")
        }
    }

    private func feedbackLabel(_ text: String, color: Color, icon: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: icon).foregroundStyle(color)
            Text(text)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(color)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func fieldLabel(_ title: String) -> some View {
        Text(title)
            .font(SCTypography.metaMonoSmall)
            .tracking(1.05)
            .foregroundStyle(Color.scInkMuted)
    }

    // MARK: - Actions

    private func connectKey() async {
        let key = keyDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        busy = true
        defer { busy = false }
        let result = await controller.setBYOKey(vendor: vendor.rawValue, key: key)
        if !result.ok {
            if result.validation == BYOKeyResult.invalid {
                feedback = .invalid
            } else {
                feedback = .error(result.error ?? "Couldn't store the key.")
            }
            return
        }
        keyDraft = ""
        // Store-only-if-valid succeeded; select it as the cloud provider (KTD2).
        _ = await controller.setCloudProvider(vendor.keyProviderID)
        switch result.validation {
        case BYOKeyResult.valid: feedback = .valid
        case BYOKeyResult.unknown: feedback = .unknown
        default: feedback = .valid
        }
    }

    private func disconnectKey() async {
        busy = true
        defer { busy = false }
        // If this vendor's key id is the active cloud provider, deselect first.
        if settings.cloudProvider == vendor.keyProviderID {
            _ = await controller.setCloudProvider(nil)
        }
        let ok = await controller.clearBYOKey(vendor: vendor.rawValue)
        feedback = ok ? .cleared : .error(controller.lastError ?? "Couldn't clear the key.")
    }

    private func selectCLI() async {
        busy = true
        defer { busy = false }
        let ok = await controller.setCloudProvider(vendor.cliProviderID)
        if ok {
            onClose()
        } else {
            feedback = .error(controller.lastError ?? "Couldn't select the provider.")
        }
    }
}
