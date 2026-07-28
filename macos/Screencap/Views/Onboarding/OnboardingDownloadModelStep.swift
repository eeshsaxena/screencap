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
        .onAppear {
            // U7 catch-up (honest status): reaching the onboarding intelligence step
            // counts as having made/seen the choice, so the first-recording beat does
            // not re-ask a user who went through the wizard.
            HUDHintStore().markIntelligenceChoiceSeen()
        }
    }

    @ViewBuilder
    private var downloadControl: some View {
        // SCR-293 — switching on `state` alone rendered a frozen progress bar
        // forever once status reads started failing (the controller leaves
        // `state` intact by design). The shared matrix folds in the staleness
        // signal so this step can't silently swallow it again.
        switch ModelDownloadOffer.render(
            state: download.state,
            progressStale: download.isDownloadProgressStale,
            daemonUnreachable: download.daemonUnreachable
        ) {
        case .installed:
            VStack(spacing: 14) {
                Text("Model installed — your day's tasks will be named on this Mac.")
                    .font(SCTypography.sans(size: 13))
                    .foregroundStyle(Color.scTeal)
                OnboardingPrimaryButton(title: "Continue", enabled: true, action: onContinue)
            }
        case let .downloading(fraction):
            VStack(spacing: 10) {
                ProgressView(value: fraction)
                    .frame(width: 280)
                Text("Downloading…").font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                OnboardingLinkButton(title: "Cancel", action: { Task { await download.cancel() } })
            }
        case .stalled:
            VStack(spacing: 10) {
                Text(ModelDownloadOffer.stalledReason)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scRust)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                // Retry re-reads status rather than restarting the download: the
                // transfer may well still be running daemon-side, and a success
                // re-arms the poll loop (KTD8) that a failed Cancel had stopped.
                OnboardingPrimaryButton(title: "Retry", enabled: true,
                                        action: { Task { await download.refreshStatus() } })
            }
            .frame(maxWidth: 380)
        case .unavailable:
            VStack(spacing: 10) {
                OnboardingPrimaryButton(title: downloadTitle, enabled: false, action: {})
                Text(ModelDownloadOffer.unavailableReason)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                OnboardingLinkButton(title: "Retry",
                                     action: { Task { await download.refreshStatus() } })
            }
            .frame(maxWidth: 380)
        case let .failed(reason):
            VStack(spacing: 10) {
                Text("Download failed: \(reason)")
                    .font(SCTypography.sans(size: 12)).foregroundStyle(Color.scRust)
                OnboardingPrimaryButton(title: "Retry", enabled: true,
                                        action: { Task { await download.startDownload() } })
            }
        case .offer:
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
