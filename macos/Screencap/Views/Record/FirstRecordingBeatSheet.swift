import SwiftUI

/// U7 (honest status): the one-time first-recording beat. Catch-up — fired from the
/// record path (non-blocking) for a user who didn't make the intelligence choice in
/// onboarding. Adaptive per the composed verdict (U5): it **waits** while the verdict
/// is unresolved (never showing a choose-a-model prompt to a user whose model is
/// already ready, AE2), shows a light confirm when a model is usable, else the
/// choose-a-model offer with the download-in-place / connect / skip paths (R5).
struct FirstRecordingBeatSheet: View {
    @Binding var isPresented: Bool
    @EnvironmentObject private var intelligence: IntelligenceController
    @StateObject private var download = ModelDownloadController()
    /// Deep-link to the Intelligence pane (R5 secondary path).
    var onOpenIntelligence: () -> Void = {}

    private var verdict: IntelligenceVerdict? {
        IntelligenceVerdict.compose(probe: OnDeviceModelStatus.probe(), settings: intelligence.settings)
    }
    private var mode: IntelligenceSurfacePolicy.BeatMode {
        IntelligenceSurfacePolicy.beatMode(verdict: verdict)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            content
        }
        .padding(24)
        .frame(width: 460)
        .background(Color.scSurface)
        .task { await intelligence.refresh() }
        .task { await download.refreshStatus() }
    }

    @ViewBuilder
    private var content: some View {
        switch mode {
        case .awaitVerdict:
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("Setting up intelligence…")
                    .font(SCTypography.sans(size: 13))
                    .foregroundStyle(Color.scInkSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 20)
        case .lightConfirm:
            Text("Intelligence is ready")
                .font(SCTypography.sans(size: 16, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Text("Screencap turns this recording into named tasks and a summary on this Mac — nothing leaves.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Got it") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        case .chooseModel:
            Text("Turn recordings into tasks")
                .font(SCTypography.sans(size: 16, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Text("Pick how Screencap names your recordings. You can change this later in Settings → Intelligence.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            downloadControl
            Button("Connect your own model") { onOpenIntelligence(); dismiss() }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12, weight: .medium))
                .foregroundStyle(Color.scTeal)
            HStack {
                Spacer()
                Button("Not now") { dismiss() }
                    .buttonStyle(.plain)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
            }
        }
    }

    @ViewBuilder
    private var downloadControl: some View {
        switch download.state {
        case .installed:
            Label("On-device model installed", systemImage: "checkmark.circle")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scTeal)
        case let .downloading(done, total):
            VStack(alignment: .leading, spacing: 6) {
                ProgressView(value: total > 0 ? Double(done) / Double(total) : nil)
                Button("Cancel") { Task { await download.cancel() } }
                    .buttonStyle(.plain)
                    .font(SCTypography.sans(size: 11))
                    .foregroundStyle(Color.scInkMuted)
            }
        case let .failed(reason):
            VStack(alignment: .leading, spacing: 6) {
                Text("Download failed: \(reason)")
                    .font(SCTypography.sans(size: 11.5))
                    .foregroundStyle(Color.scRust)
                Button("Retry") { Task { await download.startDownload() } }
            }
        case .idle, .cancelled:
            Button(downloadTitle) { Task { await download.startDownload() } }
                .keyboardShortcut(.defaultAction)
        }
    }

    private var downloadTitle: String {
        if let bytes = download.disclosedSizeBytes, bytes > 0 {
            return "Download on-device model (\(ShellSidebarModel.formatStorage(bytes)))"
        }
        return "Download on-device model (~2 GB)"
    }

    private func dismiss() { isPresented = false }
}
