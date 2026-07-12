import AppKit
import SwiftUI

/// U12 — the prototype Privacy settings pane (design 473–515): the keep-local
/// toggle wired to `upload_default` (KTD-11), the E2EE opt-in beta toggle
/// wired to the `e2ee` verb group behind the R4 limits disclosure (SCR-220
/// U4), the always-on mask row with an App-rules disclosure, the
/// private-window stub (SCR-224), and the live storage row ("Change…" is
/// SCR-228). All writes flow through the CLI settings layer (R8) via
/// `PrivacyController`.
///
/// The "Finish setup" recovery banner carries over from the legacy pane
/// (SCR-143) — it stays the always-available way back into the permission
/// walkthrough after a skipped setup.
struct PrivacySettingsView: View {
    @EnvironmentObject private var privacy: PrivacyController
    @EnvironmentObject private var permissions: PermissionController
    @EnvironmentObject private var index: RecordingsIndex
    /// SCR-260: the E2EE enable gate reads sign-in + cloud-plan state from here.
    @EnvironmentObject private var auth: CloudAuthController

    /// The mask row's disclosure — App rules is the per-app view of what the
    /// policy engine masks and blocks.
    let onOpenAppRules: () -> Void

    /// Inline error under the keep-local row after a failed CLI write (the
    /// optimistic flip has already been reverted by the controller).
    @State private var keepLocalError: String?
    /// Locks the keep-local toggle while a write round-trips (the AppRulesView
    /// pendingSegments pattern). Without it a double-tap hits the controller's
    /// in-flight guard, whose `false` return would render as a false
    /// "Couldn't save" error for a write that actually succeeded.
    @State private var keepLocalWriteInFlight = false

    /// SCR-220 U4: inline error under the E2EE row after a failed `e2ee`
    /// write (the optimistic flip has already been reverted).
    @State private var e2eeError: String?
    /// Locks the E2EE toggle while a write round-trips (same rationale as
    /// `keepLocalWriteInFlight`).
    @State private var e2eeWriteInFlight = false
    /// Drives the R4 limits disclosure that gates the off→on flip.
    @State private var showE2EEConfirm = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            finishSetupBanner
            Text(PrivacySettingsCopy.paneTitle)
                .font(SCTypography.paneHeading)
                .foregroundStyle(Color.scInk)
                .padding(.bottom, 6)
            Text(PrivacySettingsCopy.paneSub)
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInkMuted)
                .padding(.bottom, 20)

            VStack(alignment: .leading, spacing: 0) {
                keepLocalRow
                rowDivider
                e2eeRow
                rowDivider
                maskRow
                rowDivider
                pauseRow
                rowDivider
                StorageRow()
            }
            .frame(maxWidth: 720)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 30)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color.scCanvas)
        .task {
            await privacy.refreshStatus()
            // SCR-260/SCR-241: coalesced, no-op-once-resolved refresh so the
            // E2EE gate reflects current sign-in/plan state without an eager
            // `whoami` Keychain decrypt on every pane open.
            await auth.refreshIfNeeded()
        }
        .onAppear {
            // Visiting the pane is the disclosure the first-run banner asks
            // for — same idempotent clear the legacy pane performed.
            if privacy.bannerActive {
                Task { await privacy.markSetupComplete() }
            }
        }
    }

    private var rowDivider: some View {
        Rectangle().fill(Color.scFillSubtle).frame(height: 1)
    }

    // MARK: - Recovery banner (SCR-143, carried over from the legacy pane)

    @ViewBuilder
    private var finishSetupBanner: some View {
        if permissions.shouldShowFinishSetupBanner {
            HStack(spacing: 12) {
                Image(systemName: "exclamationmark.shield")
                    .font(.system(size: 18))
                    .foregroundStyle(Color.scAmberText)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Finish permission setup")
                        .font(SCTypography.sans(size: 13.5, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    Text("Grant Screen Recording and Accessibility to enable recording.")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkMuted)
                }
                Spacer(minLength: 8)
                Button("Finish setup") {
                    permissions.requestReopenSetup()
                }
                .buttonStyle(.borderedProminent)
            }
            .padding(12)
            .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
            .frame(maxWidth: 720)
            .padding(.bottom, 16)
        }
    }

    // MARK: - Keep-local row (KTD-11)

    private var keepLocalRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(PrivacySettingsCopy.keepLocalTitle)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    Text(PrivacySettingsPolicy.keepLocalCaption(uploadDefault: privacy.uploadDefault))
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scInkMuted)
                }
                Spacer(minLength: 8)
                SettingsToggle(
                    on: PrivacySettingsPolicy.keepLocalToggleOn(uploadDefault: privacy.uploadDefault),
                    action: toggleKeepLocal
                )
                .disabled(keepLocalWriteInFlight)
            }
            if let keepLocalError {
                Text(keepLocalError)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scRust)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 16)
    }

    private func toggleKeepLocal() {
        guard !keepLocalWriteInFlight else { return }
        let target = !PrivacySettingsPolicy.keepLocalToggleOn(uploadDefault: privacy.uploadDefault)
        keepLocalError = nil
        keepLocalWriteInFlight = true
        Task {
            let ok = await privacy.setUploadDefault(
                PrivacySettingsPolicy.uploadDefaultValue(togglingTo: target)
            )
            keepLocalWriteInFlight = false
            if !ok {
                keepLocalError = privacy.lastError.map { "Couldn't save: \($0)" }
                    ?? "Couldn't save the setting."
            }
        }
    }

    // MARK: - E2EE row (SCR-220 U4 — opt-in beta toggle, R4 disclosure gate;
    // SCR-260 — cloud-capability gate on the enable path)

    /// SCR-260: cloud-eligibility for the E2EE enable gate, from the app's auth
    /// signals. `.unknown` auth (pre-first-check) resolves to `signedOut` here,
    /// which gates the row (safe default); `e2eeCaptionText` softens that window.
    /// Kept as plain computed state (not in a view builder) so the type-checker
    /// solver stays well clear of the e2eeRowHeader fragility.
    private var e2eeEligibility: PrivacySettingsPolicy.E2EECloudEligibility {
        PrivacySettingsPolicy.e2eeEligibility(
            isSignedIn: auth.isSignedIn,
            cloudCapable: auth.isSubscribed,
            planStale: auth.status.isStale,
            paywallEnabled: auth.paywallEnabled
        )
    }

    /// The tap outcome for the current flag + eligibility.
    private var e2eeOutcome: PrivacySettingsPolicy.E2EETapOutcome {
        PrivacySettingsPolicy.e2eeTapOutcome(
            cloudE2EEEnabled: privacy.cloudE2EEEnabled,
            eligibility: e2eeEligibility
        )
    }

    /// The row is non-interactive when locked (older CLI) or gated (SCR-260).
    private var e2eeNonInteractive: Bool {
        e2eeOutcome == .locked || e2eeOutcome == .gated
    }

    /// The caption, eligibility-keyed — with a neutral "checking" caption during
    /// the pre-first-check `.unknown` window so an already-eligible user never
    /// flashes the signed-out "sign in" copy.
    private var e2eeCaptionText: String {
        if privacy.cloudE2EEEnabled == false, case .unknown = auth.status {
            return PrivacySettingsCopy.e2eeSubChecking
        }
        return PrivacySettingsPolicy.e2eeCaption(
            cloudE2EEEnabled: privacy.cloudE2EEEnabled,
            eligibility: e2eeEligibility
        )
    }

    /// Hover help, eligibility-keyed (gated rows get gated help, not the
    /// off-state "turn on to encrypt" copy).
    private var e2eeHelpText: String {
        PrivacySettingsPolicy.e2eeHelp(
            cloudE2EEEnabled: privacy.cloudE2EEEnabled,
            eligibility: e2eeEligibility
        )
    }

    private var e2eeRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            e2eeRowHeader
            if let e2eeError {
                Text(e2eeError)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scRust)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 16)
        .opacity(e2eeNonInteractive ? 0.75 : 1)
        .help(e2eeHelpText)
        .confirmationDialog(
            PrivacySettingsCopy.e2eeConfirmTitle,
            isPresented: $showE2EEConfirm,
            titleVisibility: .visible
        ) {
            Button(PrivacySettingsCopy.e2eeConfirmAction) { confirmEnableE2EE() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(PrivacySettingsCopy.e2eeConfirmBody)
        }
    }

    // Extracted from `e2eeRow` so each view builder stays small enough for the
    // Swift type-checker to resolve — the full inline chain (VStack → HStack →
    // VStack → HStack + toggle + trailing `.confirmationDialog`) crashed the
    // solver ("failed to produce diagnostic for expression"), failing the
    // universal Release build.
    private var e2eeRowHeader: some View {
        let state = privacy.cloudE2EEEnabled
        // Non-interactive when locked (older CLI, KTD-8) OR gated (SCR-260 —
        // not cloud-capable): both render a dimmed row with a nil-action toggle.
        let nonInteractive = e2eeNonInteractive
        // A ternary between `nil` and the unapplied method reference
        // `toggleE2EE` is what crashed the solver ("failed to produce
        // diagnostic for expression") and failed the Release build. A plain
        // `if` plus an explicit closure literal avoids both the ternary and the
        // bare method reference.
        let toggleAction: (() -> Void)?
        if nonInteractive {
            toggleAction = nil
        } else {
            toggleAction = { toggleE2EE() }
        }
        return HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(PrivacySettingsCopy.e2eeTitle)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    chip(
                        PrivacySettingsPolicy.e2eeChip(cloudE2EEEnabled: state),
                        color: nonInteractive ? .scInkMuted : .scTeal
                    )
                }
                Text(e2eeCaptionText)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            // Locked (nil flag — older CLI without the `e2ee` verb,
            // KTD-8): non-interactive stub presentation, never a toggle
            // whose write path may not exist.
            SettingsToggle(
                on: PrivacySettingsPolicy.e2eeToggleOn(cloudE2EEEnabled: state),
                action: toggleAction
            )
            .disabled(e2eeWriteInFlight)
        }
    }

    /// R4 gate: the off→on tap only presents the limits disclosure — the
    /// switch stays OFF and no CLI call fires until the sheet's confirm
    /// (cancel dismisses with the switch untouched). The on→off tap needs no
    /// disclosure and flips optimistically.
    private func toggleE2EE() {
        guard !e2eeWriteInFlight else { return }
        e2eeError = nil
        // Uses the eligibility-aware outcome: a gated row's toggle already has a
        // nil action, so this is defense-in-depth — a gated (or locked) tap is a
        // no-op, never a disclosure or a KEK-creating write (SCR-260).
        switch e2eeOutcome {
        case .locked, .gated:
            return
        case .showDisclosure:
            showE2EEConfirm = true
        case .disable:
            runE2EEWrite(false)
        }
    }

    /// The disclosure's confirm — only now does the optimistic flip + CLI
    /// call happen (`e2ee enable` creates the key before setting the flag,
    /// KTD-3; a failure reverts and surfaces inline, R2).
    private func confirmEnableE2EE() {
        runE2EEWrite(true)
    }

    private func runE2EEWrite(_ on: Bool) {
        e2eeWriteInFlight = true
        Task {
            let ok = await privacy.setCloudE2EE(on)
            e2eeWriteInFlight = false
            if !ok {
                e2eeError = privacy.lastError.map { "Couldn't save: \($0)" }
                    ?? "Couldn't save the setting."
            }
        }
    }

    // MARK: - Mask row (always-on policy engine; per-app overrides SCR-225)

    private var maskRow: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(PrivacySettingsCopy.maskTitle)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    chip(PrivacySettingsCopy.maskChip, color: .scTeal)
                }
                HStack(spacing: 4) {
                    Text(PrivacySettingsCopy.maskSub)
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scInkMuted)
                    Button(action: onOpenAppRules) {
                        Text(PrivacySettingsCopy.maskLink)
                            .font(SCTypography.sans(size: 12.5))
                            .underline()
                            .foregroundStyle(Color.scTeal)
                    }
                    .buttonStyle(.plain)
                }
            }
            Spacer(minLength: 8)
            // Locked-on: the policy engine cannot be switched off (SCR-225 is
            // the per-app override path, not a global kill switch).
            SettingsToggle(on: true, action: nil)
        }
        .padding(.vertical, 16)
        .help(PrivacySettingsCopy.maskHelp)
    }

    // MARK: - Private-window row (stub: SCR-224 private-window auto-pause)

    private var pauseRow: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(PrivacySettingsCopy.pauseTitle)
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(PrivacySettingsCopy.pauseSub)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            SettingsToggle(on: false, action: nil)
        }
        .padding(.vertical, 16)
        .opacity(0.75)
        .help(PrivacySettingsCopy.pauseHelp)
    }

    // MARK: - Storage row (SCR-228 — live path + working "Change…")
    // The row itself is `StorageRow` (below): it owns the folder picker,
    // confirmation, and migration progress/result state.

    static func abbreviateHome(_ path: String) -> String {
        (path as NSString).abbreviatingWithTildeInPath
    }

    private func chip(_ label: String, color: Color) -> some View {
        Text(label)
            .font(SCTypography.mono(size: 9.5))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .overlay(Capsule().strokeBorder(color.opacity(0.35), lineWidth: 1))
    }
}

/// The design's 38×22 pill toggle (logic 737–742). `action == nil` renders it
/// locked (non-interactive) — used for the always-on mask row and the stub
/// rows/sheets, which display a state but take no input. `onFill` covers the
/// design's two on-state fills: live teal, and soft teal for stubbed "on"
/// states (the sheet's MCP row, the team-setup domain toggle).
struct SettingsToggle: View {
    let on: Bool
    var onFill: Color = .scTeal
    let action: (() -> Void)?

    var body: some View {
        Button {
            action?()
        } label: {
            Capsule()
                .fill(on ? onFill : Color.scBorderWarm)
                .frame(width: 38, height: 22)
                .overlay(alignment: on ? .trailing : .leading) {
                    Circle().fill(Color.scPaper).frame(width: 18, height: 18).padding(2)
                }
                .animation(.easeInOut(duration: 0.15), value: on)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(action == nil)
        .accessibilityValue(on ? "on" : "off")
    }
}

/// SCR-228 storage row: the live recordings path + size, and a working
/// "Change…" that picks a same-disk folder, confirms the move, and drives the
/// migration progress/result surface via `PrivacyController.migrationState`.
/// Its own struct (not a computed property on the pane) so it can own the
/// picker + confirmation `@State`.
private struct StorageRow: View {
    @EnvironmentObject private var privacy: PrivacyController
    @EnvironmentObject private var index: RecordingsIndex

    @State private var pendingURL: URL?
    @State private var showConfirm = false

    private var isMigrating: Bool {
        if case .migrating = privacy.migrationState { return true }
        return false
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(PrivacySettingsCopy.storageTitle)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    Text(storageLine)
                        .font(SCTypography.mono(size: 12))
                        .foregroundStyle(Color.scInkMuted)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 8)
                changeButton
            }
            migrationStatus
        }
        .padding(.vertical, 16)
        .onDisappear { privacy.clearMigrationState() }  // no stale banner on return
        .confirmationDialog(
            PrivacySettingsCopy.storageConfirmTitle,
            isPresented: $showConfirm,
            titleVisibility: .visible,
            presenting: pendingURL
        ) { url in
            Button("Move Recordings") {
                pendingURL = nil
                Task { await privacy.startMigration(to: url) }
            }
            Button("Cancel", role: .cancel) { pendingURL = nil }
        } message: { url in
            Text(PrivacySettingsCopy.storageConfirmBody(
                target: PrivacySettingsView.abbreviateHome(url.path)
            ))
        }
    }

    private var changeButton: some View {
        Button {
            privacy.clearMigrationState()  // clear a prior result before re-engaging
            if let url = Self.pickFolder(startingAt: privacy.recordingsDir) {
                pendingURL = url
                showConfirm = true
            }
        } label: {
            Text(PrivacySettingsCopy.storageChangeLabel)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(isMigrating)
        .opacity(isMigrating ? 0.6 : 1)
        .help(PrivacySettingsCopy.storageChangeHelp)
    }

    @ViewBuilder
    private var migrationStatus: some View {
        switch privacy.migrationState {
        case .idle:
            EmptyView()
        case .migrating:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(PrivacySettingsCopy.storageMigratingLabel)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
            }
        case .succeeded:
            Text("Recordings moved.")
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scTeal)
        case .failed(_, let message):
            Text(message)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scAmberText)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// "~/.screencap/recordings · 4.2 GB" — the live configured directory
    /// (home-abbreviated) plus the recordings-index size total.
    private var storageLine: String {
        let path = privacy.recordingsDir.map(PrivacySettingsView.abbreviateHome) ?? "loading…"
        let bytes = index.recordings.reduce(0) { $0 + $1.sizeBytes }
        return "\(path) · \(ShellSidebarModel.formatStorage(bytes))"
    }

    /// Directories-only open panel, defaulting near the current recordings dir.
    private static func pickFolder(startingAt current: String?) -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = "Choose"
        panel.message = PrivacySettingsCopy.storagePickerMessage
        if let current, !current.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: current).deletingLastPathComponent()
        }
        return panel.runModal() == .OK ? panel.urls.first : nil
    }
}
