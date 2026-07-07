import SwiftUI

/// SCR-239 (U11) — the opt-in downloadable-model offer, shown after `storage`
/// for the local tier only. Non-blocking: "Not now" leaves the user on the
/// idle-gap heuristic and completes onboarding (R7); the download runs on-device
/// (nothing leaves the Mac). Both paths complete the wizard.
struct OnboardingDownloadModelStep: View {
    let onContinue: () -> Void
    let onSkip: () -> Void

    @StateObject private var download = ModelDownloadController()

    var body: some View {
        VStack(spacing: 0) {
            Text(OnboardingCopy.downloadModelHeadline)
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .padding(.bottom, 10)
            Text(OnboardingCopy.downloadModelSub)
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 460)
                .padding(.bottom, 30)

            downloadControl
                .padding(.bottom, 22)

            OnboardingLinkButton(title: OnboardingCopy.downloadModelSkip, action: onSkip)
        }
        .padding(.horizontal, 100)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task { await download.refreshStatus() }
    }

    @ViewBuilder
    private var downloadControl: some View {
        switch download.state {
        case .installed:
            VStack(spacing: 14) {
                Text("Model installed — your day's tasks will be named on this Mac.")
                    .font(SCTypography.sans(size: 13))
                    .foregroundStyle(Color.scTeal)
                OnboardingPrimaryButton(title: "Continue", enabled: true, action: onContinue)
            }
        case let .downloading(done, total):
            VStack(spacing: 10) {
                ProgressView(value: total > 0 ? Double(done) / Double(total) : nil)
                    .frame(width: 280)
                Text("Downloading…").font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                OnboardingLinkButton(title: "Cancel", action: { Task { await download.cancel() } })
            }
        case let .failed(reason):
            VStack(spacing: 10) {
                Text("Download failed: \(reason)")
                    .font(SCTypography.sans(size: 12)).foregroundStyle(Color.scRust)
                OnboardingPrimaryButton(title: "Retry", enabled: true,
                                        action: { Task { await download.startDownload() } })
            }
        case .idle, .cancelled:
            OnboardingPrimaryButton(title: downloadTitle, enabled: true,
                                    action: { Task { await download.startDownload() } })
        }
    }

    private var downloadTitle: String {
        if let bytes = download.disclosedSizeBytes, bytes > 0 {
            return "Download (\(ShellSidebarModel.formatStorage(bytes)))"
        }
        return "Download the model (~2 GB)"
    }
}
