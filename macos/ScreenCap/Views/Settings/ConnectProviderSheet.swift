import SwiftUI

/// U4 — the sequential add-provider flow (R6–R9; F2). An in-window overlay (the
/// app's `NewRecordingSheet` pattern, KTD4) rather than a native `.sheet`,
/// restructured from the old two-picker sheet into steps driven by the pure
/// `ConnectProviderStepPolicy`:
///
/// 1. **Pick** — OpenAI / Anthropic / Gemini / Local server (R6). Add-new entry
///    starts here; a Manage/Set-up entry skips straight to configure with its
///    choice preset (and offers no Back — there is no pick step behind it).
/// 2. **Configure** — per mechanism:
///    - **API key**: a `SecureField`. The secret is handed to
///      `IntelligenceController.setBYOKey`, which pipes it to the daemon over
///      STDIN of `settings intelligence --set-key <vendor> --validate` — NEVER
///      an argv element. Verdicts (valid / invalid / unknown) render distinct
///      inline states; Replace & connect and Disconnect are preserved for the
///      manage entry (disconnect of the selected vendor deselects FIRST —
///      selection returns to on-device — then clears the key).
///    - **CLI delegation**: the availability check, with honest copy stating no
///      test call is made (KTD5); unavailable renders the needs-attention
///      surface with install/sign-in guidance.
///    - **Local server**: URL field + Save via `setEndpoint`, then the daemon's
///      LOCAL/REMOTE classification renders as the result caption (AE3). The
///      draft is re-seeded on step ENTRY (never the sheet's `.onAppear`). A
///      clear — or any re-save — while `local-server` is the persisted provider
///      resets `provider` to on-device FIRST (the plan's ordering rule).
/// 3. **Done** — reachable only after a terminal configure action. Finishing
///    auto-selects the added row through the controller seams ONLY when it is
///    selectable (R8: a REMOTE server or needs-attention CLI add finishes
///    unselected), then reports the row id to the pane via `onComplete` so the
///    pane sets the just-added highlight.
///
/// No eager credential probes happen on open — keys are daemon-owned and only
/// presence flags are read back (R3 trust boundary). Copy is honest (R12): no
/// "free unlimited" / "we can't see it" / E2EE strings, and nothing claims a
/// cloud pick replaces the local answerer.
struct ConnectProviderSheet: View {
    let entry: ConnectFlowEntry
    let settings: IntelligenceSettings
    let controller: IntelligenceController
    let onClose: () -> Void
    /// Flow completion (R8): reports the added row id so the pane can set the
    /// just-added highlight. Any auto-select has already been issued.
    let onComplete: (String) -> Void

    @State private var step: ConnectFlowStep
    @State private var mechanism: BYOMechanism = .apiKey
    @State private var keyDraft: String = ""
    /// Local-server URL draft — re-seeded on ENTRY to the local-server
    /// configure step (init for a manage entry, `advance(to:)` from pick), so
    /// re-entering the step always shows the persisted endpoint, never a stale
    /// draft from an earlier visit.
    @State private var endpointDraft: String = ""
    @State private var busy = false
    /// The last write's result surfaced inline (valid/invalid/unknown/error).
    @State private var feedback: Feedback?
    /// The endpoint classification read back after a save (the AE3 caption).
    @State private var savedClassification: String?
    /// Minted by a successful terminal configure action — the only way Done
    /// becomes reachable (the step policy's rule, held as state).
    @State private var pendingCompletion: ConnectProviderStepPolicy.Completion?

    /// Keyboard focus, moved to the step's field on entry so the tab order
    /// starts in the right place across step changes.
    @FocusState private var focusedField: FocusField?
    enum FocusField: Hashable {
        case key
        case url
    }

    init(
        entry: ConnectFlowEntry,
        settings: IntelligenceSettings,
        controller: IntelligenceController,
        onClose: @escaping () -> Void,
        onComplete: @escaping (String) -> Void
    ) {
        self.entry = entry
        self.settings = settings
        self.controller = controller
        self.onClose = onClose
        self.onComplete = onComplete
        let initial = ConnectProviderStepPolicy.initialStep(for: entry)
        _step = State(initialValue: initial)
        switch initial {
        case .configure(.vendor(let vendor)):
            // A manage entry for a vendor whose CLI is the trouble (no key
            // stored either) opens on the CLI tab — that's the row it came from.
            if !settings.cliAvailable(forProviderID: vendor.cliProviderID),
               !settings.keyPresent(forVendor: vendor.rawValue) {
                _mechanism = State(initialValue: .cli)
            }
        case .configure(.localServer):
            // Step-entry seeding (the manage entry's "entry" is init itself).
            _endpointDraft = State(initialValue: settings.localServerEndpoint ?? "")
        case .pick:
            break
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
        .onAppear { focusStep(step) }
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
                stepBody
                if let feedback { feedbackRow(feedback) }
                if pendingCompletion != nil { doneRow }
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
            if ConnectProviderStepPolicy.canGoBack(from: step, entry: entry) {
                Button {
                    goBack()
                } label: {
                    HStack(spacing: 3) {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 11, weight: .semibold))
                        Text(ConnectProviderModel.flowBackButtonTitle)
                            .font(SCTypography.sans(size: 12, weight: .semibold))
                    }
                    .foregroundStyle(Color.scInkMuted)
                }
                .buttonStyle(.plain)
                .disabled(busy)
            }
            Text(stepTitle)
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
                .accessibilityAddTraits(.isHeader)
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

    private var stepTitle: String {
        switch step {
        case .pick:
            return ConnectProviderModel.flowPickStepTitle
        case .configure(let choice):
            return ConnectProviderModel.flowConfigureStepTitle(for: choice)
        }
    }

    // MARK: - Step switch

    @ViewBuilder
    private var stepBody: some View {
        switch step {
        case .pick:
            pickStep
        case .configure(let choice):
            configureStep(choice)
        }
    }

    // MARK: - Pick step (R6)

    private var pickStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(ConnectProviderModel.flowPickCaption)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 0) {
                let choices = ConnectProviderModel.flowChoices
                ForEach(choices) { choice in
                    Button {
                        advance(to: ConnectProviderStepPolicy.step(afterPicking: choice))
                    } label: {
                        HStack(alignment: .center, spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(choice.displayName)
                                    .font(SCTypography.sans(size: 14, weight: .semibold))
                                    .foregroundStyle(Color.scInk)
                                Text(choiceCaption(choice))
                                    .font(SCTypography.sans(size: 12))
                                    .foregroundStyle(Color.scInkMuted)
                            }
                            Spacer(minLength: 8)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(Color.scInkMuted)
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 12)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                    if choice != choices.last {
                        Rectangle().fill(Color.scFillSubtle).frame(height: 1)
                    }
                }
            }
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
        }
    }

    private func choiceCaption(_ choice: ProviderChoice) -> String {
        switch choice {
        case .vendor:
            return ConnectProviderModel.vendorChoiceCaption
        case .localServer:
            return ConnectProviderModel.localServerChoiceCaption
        }
    }

    // MARK: - Configure step

    @ViewBuilder
    private func configureStep(_ choice: ProviderChoice) -> some View {
        switch choice {
        case .vendor(let vendor):
            mechanismPicker
            switch mechanism {
            case .apiKey: apiKeyBody(vendor)
            case .cli: cliBody(vendor)
            }
        case .localServer:
            localServerBody
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
            .onChange(of: mechanism) { newValue in
                feedback = nil
                pendingCompletion = nil
                focusedField = newValue == .apiKey ? .key : nil
            }
        }
    }

    // MARK: - API-key mechanism (R7)

    private func apiKeyBody(_ vendor: BYOVendor) -> some View {
        let connected = settings.keyPresent(forVendor: vendor.rawValue)
        return VStack(alignment: .leading, spacing: 10) {
            Text(vendor.keyBillingCopy)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)

            // SecureField — the key is never rendered and never becomes an argv
            // element; `setBYOKey` pipes it over STDIN.
            SecureField(connected ? "Enter a new key to replace the stored one" : "Paste your \(vendor.displayName) API key", text: $keyDraft)
                .textFieldStyle(.roundedBorder)
                .font(SCTypography.mono(size: 12))
                .focused($focusedField, equals: .key)
                .disabled(busy)

            HStack(spacing: 10) {
                Button(connected ? "Replace & connect" : "Connect") {
                    Task { await connectKey(vendor) }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(busy || keyDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                if connected {
                    Button("Disconnect") {
                        Task { await disconnectKey(vendor) }
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

    // MARK: - CLI-delegation mechanism (KTD5)

    private func cliBody(_ vendor: BYOVendor) -> some View {
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
                    Task { await selectCLI(vendor) }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(busy)
            } else {
                // The needs-attention surface: the honest fix path, never a
                // silent substitution of another provider.
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
            // KTD5 — honest about what the check is: a local stat-check only.
            Text(ConnectProviderModel.cliAvailabilityHonestCopy)
                .font(SCTypography.sans(size: 11.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)
            if busy { ProgressView().controlSize(.small) }
        }
    }

    // MARK: - Local-server mechanism (AE3/R9)

    private var localServerBody: some View {
        let endpointSet = !(settings.localServerEndpoint ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return VStack(alignment: .leading, spacing: 10) {
            Text(ConnectProviderModel.localServerConfigureCaption)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)

            fieldLabel(ConnectProviderModel.endpointFieldLabel)
            TextField(ConnectProviderModel.endpointFieldPlaceholder, text: $endpointDraft)
                .textFieldStyle(.roundedBorder)
                .font(SCTypography.mono(size: 12))
                .focused($focusedField, equals: .url)
                .disabled(busy)

            HStack(spacing: 10) {
                Button(ConnectProviderModel.endpointSaveButtonTitle) {
                    Task { await saveEndpoint() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(busy || endpointDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                if endpointSet {
                    Button(ConnectProviderModel.endpointClearButtonTitle) {
                        Task { await clearEndpoint() }
                    }
                    .controlSize(.small)
                    .disabled(busy)
                }

                if busy { ProgressView().controlSize(.small) }
                Spacer(minLength: 0)
            }

            if let savedClassification {
                classificationCaption(savedClassification)
            }
        }
    }

    /// The post-save classification caption (AE3): LOCAL states what can use
    /// the server; anything else renders the REMOTE consequence — the same
    /// string the pane's non-selectable row shows (R9).
    @ViewBuilder
    private func classificationCaption(_ classification: String) -> some View {
        if classification == "LOCAL" {
            feedbackLabel(
                ConnectProviderModel.endpointLocalResultCopy,
                color: Color.scTeal, icon: "checkmark.seal"
            )
        } else {
            feedbackLabel(
                IntelligenceSelectionModel.remoteEndpointNotSelectableCopy,
                color: Color.scAmberText, icon: "exclamationmark.triangle.fill"
            )
        }
    }

    // MARK: - Done (terminal transition)

    /// Rendered only while a terminal configure action's completion is pending
    /// (the step policy's done-only-after-terminal-action rule).
    private var doneRow: some View {
        HStack {
            Spacer(minLength: 0)
            Button(ConnectProviderModel.flowDoneButtonTitle) {
                if let completion = pendingCompletion { finish(completion) }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(busy)
        }
    }

    /// Report the added row id to the pane (which sets the just-added
    /// highlight, R8) and close. Any auto-select already ran at the terminal
    /// action's edge.
    private func finish(_ completion: ConnectProviderStepPolicy.Completion) {
        onComplete(completion.addedRowID)
        onClose()
    }

    // MARK: - Feedback

    @ViewBuilder
    private func feedbackRow(_ feedback: Feedback) -> some View {
        switch feedback {
        case .valid:
            feedbackLabel(ConnectProviderModel.keyVerdictValidCopy,
                          color: Color.scTeal, icon: "checkmark.circle.fill")
        case .invalid:
            feedbackLabel(ConnectProviderModel.keyVerdictInvalidCopy(vendorName: stepVendorName),
                          color: Color.scRust, icon: "xmark.octagon.fill")
        case .unknown:
            feedbackLabel(ConnectProviderModel.keyVerdictUnknownCopy(vendorName: stepVendorName),
                          color: Color.scAmberText, icon: "questionmark.circle.fill")
        case let .error(msg):
            feedbackLabel(msg, color: Color.scRust, icon: "exclamationmark.triangle.fill")
        case .cleared:
            feedbackLabel(ConnectProviderModel.disconnectedFeedbackCopy,
                          color: Color.scInkMuted, icon: "minus.circle")
        }
    }

    /// The vendor the configure step is showing — feedback is only ever set
    /// within the step whose action produced it.
    private var stepVendorName: String {
        if case .configure(.vendor(let vendor)) = step { return vendor.displayName }
        return "the provider"
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

    // MARK: - Step navigation

    /// Move to `newStep`, seeding its drafts on ENTRY (the endpoint draft rule)
    /// and moving keyboard focus to its field.
    private func advance(to newStep: ConnectFlowStep) {
        feedback = nil
        pendingCompletion = nil
        savedClassification = nil
        switch newStep {
        case .configure(.vendor):
            keyDraft = ""
            mechanism = .apiKey
        case .configure(.localServer):
            // Re-seeded on step ENTRY — never the sheet's `.onAppear`.
            endpointDraft = settings.localServerEndpoint ?? ""
        case .pick:
            break
        }
        step = newStep
        focusStep(newStep)
    }

    private func goBack() {
        guard let target = ConnectProviderStepPolicy.stepAfterBack(from: step, entry: entry)
        else { return }
        advance(to: target)
    }

    private func focusStep(_ step: ConnectFlowStep) {
        switch step {
        case .configure(.vendor):
            focusedField = mechanism == .apiKey ? .key : nil
        case .configure(.localServer):
            focusedField = .url
        case .pick:
            focusedField = nil
        }
    }

    // MARK: - Terminal actions

    /// Issue a completion's auto-select through the controller seams (R8 —
    /// only selectable rows carry one). Returns success.
    private func performAutoSelect(
        _ autoSelect: ConnectProviderStepPolicy.AutoSelect
    ) async -> Bool {
        switch autoSelect {
        case .cloudRow(let id):
            return await controller.selectCloudProvider(id)
        case .localRow(let provider):
            return await controller.selectLocalProvider(provider)
        case .none:
            return true
        }
    }

    private func connectKey(_ vendor: BYOVendor) async {
        let key = keyDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        busy = true
        defer { busy = false }
        let result = await controller.setBYOKey(vendor: vendor.rawValue, key: key)
        if !result.ok {
            if result.validation == BYOKeyResult.invalid {
                feedback = .invalid
            } else {
                feedback = .error(result.error ?? ConnectProviderModel.keyStoreFailedFallback)
            }
            pendingCompletion = nil
            return
        }
        keyDraft = ""
        // Stored (valid or unverified-unknown) — the row is selectable, so the
        // completion auto-selects it now; Done then reports + closes.
        let completion = ConnectProviderStepPolicy.completion(afterKeyStored: vendor)
        guard await performAutoSelect(completion.autoSelect) else {
            feedback = .error(controller.lastError ?? ConnectProviderModel.providerSelectFailedFallback)
            return
        }
        feedback = result.validation == BYOKeyResult.unknown ? .unknown : .valid
        pendingCompletion = completion
    }

    /// Disconnect the vendor's stored key. Ordered by the pure plan: if this
    /// vendor's key row is the persisted `cloud_provider`, deselect FIRST —
    /// selection visibly returns to on-device — then clear the key.
    private func disconnectKey(_ vendor: BYOVendor) async {
        busy = true
        defer { busy = false }
        pendingCompletion = nil
        for operation in ConnectProviderStepPolicy.disconnectPlan(
            vendor: vendor, persistedCloudProvider: settings.cloudProvider
        ) {
            switch operation {
            case .selectOnDevice:
                guard await controller.selectLocalProvider(IntelligenceSelectionModel.onDeviceRowID) else {
                    feedback = .error(controller.lastError ?? ConnectProviderModel.providerSelectFailedFallback)
                    return
                }
            case .clearKey(let vendorID):
                guard await controller.clearBYOKey(vendor: vendorID) else {
                    feedback = .error(controller.lastError ?? ConnectProviderModel.keyClearFailedFallback)
                    return
                }
            }
        }
        feedback = .cleared
    }

    private func selectCLI(_ vendor: BYOVendor) async {
        busy = true
        defer { busy = false }
        let completion = ConnectProviderStepPolicy.completion(
            afterCLIConfirmed: vendor, cliAvailable: true
        )
        if await performAutoSelect(completion.autoSelect) {
            // The availability check WAS the inline confirmation — finish now.
            finish(completion)
        } else {
            feedback = .error(controller.lastError ?? ConnectProviderModel.providerSelectFailedFallback)
        }
    }

    /// Save the endpoint draft. Ordered by the pure plan: while `local-server`
    /// is the persisted provider the flow resets to on-device FIRST (the new
    /// URL's classification is only known after the daemon round-trip); a LOCAL
    /// result re-selects via the completion's auto-select, a REMOTE one
    /// finishes unselected with the consequence stated (AE3/R9).
    private func saveEndpoint() async {
        let url = endpointDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty else { return }
        busy = true
        defer { busy = false }
        feedback = nil
        savedClassification = nil
        pendingCompletion = nil
        guard await runEndpointPlan(newValue: url) else { return }
        // `setEndpoint` reconciled against disk — read the daemon's verdict.
        let classification = controller.settings?.endpointClassification
        savedClassification = classification ?? "REMOTE"
        let completion = ConnectProviderStepPolicy.completion(afterEndpointSaved: classification)
        guard await performAutoSelect(completion.autoSelect) else {
            feedback = .error(controller.lastError ?? ConnectProviderModel.providerSelectFailedFallback)
            return
        }
        pendingCompletion = completion
    }

    /// Clear the endpoint (removes the row). Same provider-reset-first plan as
    /// a save; no completion — there is no added row to highlight.
    private func clearEndpoint() async {
        busy = true
        defer { busy = false }
        feedback = nil
        savedClassification = nil
        pendingCompletion = nil
        guard await runEndpointPlan(newValue: "") else { return }
        endpointDraft = ""
        feedback = .cleared
    }

    private func runEndpointPlan(newValue: String) async -> Bool {
        for operation in ConnectProviderStepPolicy.endpointWritePlan(
            newValue: newValue, persistedProvider: settings.provider
        ) {
            switch operation {
            case .selectOnDevice:
                guard await controller.selectLocalProvider(IntelligenceSelectionModel.onDeviceRowID) else {
                    feedback = .error(controller.lastError ?? ConnectProviderModel.providerSelectFailedFallback)
                    return false
                }
            case .writeEndpoint(let value):
                guard await controller.setEndpoint(value) else {
                    feedback = .error(controller.lastError ?? ConnectProviderModel.endpointWriteFailedFallback)
                    return false
                }
            }
        }
        return true
    }
}
