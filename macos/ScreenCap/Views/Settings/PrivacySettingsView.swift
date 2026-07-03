import SwiftUI

/// U12 — the prototype Privacy settings pane (design 473–515): the keep-local
/// toggle wired to `upload_default` (KTD-11), the honest E2EE row (SCR-220
/// stub), the always-on mask row with an App-rules disclosure, the
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

    /// The mask row's disclosure — App rules is the per-app view of what the
    /// policy engine masks and blocks.
    let onOpenAppRules: () -> Void

    /// Inline error under the keep-local row after a failed CLI write (the
    /// optimistic flip has already been reverted by the controller).
    @State private var keepLocalError: String?

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
                storageRow
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
        let target = !PrivacySettingsPolicy.keepLocalToggleOn(uploadDefault: privacy.uploadDefault)
        keepLocalError = nil
        Task {
            let ok = await privacy.setUploadDefault(
                PrivacySettingsPolicy.uploadDefaultValue(togglingTo: target)
            )
            if !ok {
                keepLocalError = privacy.lastError.map { "Couldn't save: \($0)" }
                    ?? "Couldn't save the setting."
            }
        }
    }

    // MARK: - E2EE row (stub: SCR-220 end-to-end encryption for shared copies)

    private var e2eeRow: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(PrivacySettingsCopy.e2eeTitle)
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    chip(PrivacySettingsCopy.e2eeChip, color: .scInkMuted)
                }
                Text(PrivacySettingsCopy.e2eeSub)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            SettingsToggle(on: false, action: nil)
        }
        .padding(.vertical, 16)
        .opacity(0.75)
        .help(PrivacySettingsCopy.e2eeHelp)
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

    // MARK: - Storage row ("Change…" stub: SCR-228 storage migration)

    private var storageRow: some View {
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
            Button {} label: {
                Text(PrivacySettingsCopy.storageChangeLabel)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkSecondary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
                    .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .disabled(true)
            .opacity(0.6)
            .help(PrivacySettingsCopy.storageChangeHelp)
        }
        .padding(.vertical, 16)
    }

    /// "~/.screencap/recordings · 4.2 GB" — the live configured directory
    /// (home-abbreviated) plus the recordings-index size total.
    private var storageLine: String {
        let path = privacy.recordingsDir.map(Self.abbreviateHome) ?? "loading…"
        let bytes = index.recordings.reduce(0) { $0 + $1.sizeBytes }
        return "\(path) · \(ShellSidebarModel.formatStorage(bytes))"
    }

    static func abbreviateHome(_ path: String) -> String {
        let home = NSHomeDirectory()
        guard path.hasPrefix(home) else { return path }
        return "~" + path.dropFirst(home.count)
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
/// rows, which display a state but take no input.
struct SettingsToggle: View {
    let on: Bool
    let action: (() -> Void)?

    var body: some View {
        Button {
            action?()
        } label: {
            Capsule()
                .fill(on ? Color.scTeal : Color.scBorderWarm)
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
