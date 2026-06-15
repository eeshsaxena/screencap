import SwiftUI

/// The Privacy sidebar pane (SCR-17 / U3). Header with the configured mode
/// (read-only in v1) + manual refresh button + scrollable list of installed
/// apps with per-row state badges and exclude toggles.
///
/// Refreshes on appear; live config-watcher is deferred — friend-trial
/// feedback decides whether to add one.
struct PrivacyPaneView: View {
    @EnvironmentObject private var privacy: PrivacyController
    @EnvironmentObject private var permissions: PermissionController

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            finishSetupBanner
            content
        }
        .task { await initialLoad() }
        .onAppear {
            // Pane visit alone clears the banner — this fires for users who
            // navigate via the sidebar directly instead of via the banner's
            // "Review what's captured" CTA. markSetupComplete is idempotent
            // so the dual-path is safe.
            if privacy.bannerActive {
                Task { await privacy.markSetupComplete() }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Privacy")
                    .font(.title2.bold())
                modeSubheading
            }
            Spacer()
            Button {
                Task { await privacy.refreshApps() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh app list")
            .disabled(privacy.isLoading)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    /// Recovery entry point for a skipped/incomplete first-run setup. Shown only
    /// while `setupDismissed` is latched — i.e. the user tapped "Skip for now"
    /// and setup hasn't since completed (completing it clears the flag via the
    /// daemon-grant auto-clear). This is the always-available way back into the
    /// walkthrough; without it a mistaken Skip is a dead end on the CLI-fallback
    /// path, where the launch gate never re-pops.
    @ViewBuilder
    private var finishSetupBanner: some View {
        if permissions.setupDismissed {
            HStack(spacing: 12) {
                Image(systemName: "exclamationmark.shield")
                    .font(.system(size: 18))
                    .foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Permission setup was skipped")
                        .font(.subheadline.weight(.semibold))
                    Text("ScreenCap can't record until the helper and its permissions are set up.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Finish setup") {
                    permissions.requestReopenSetup()
                }
                .buttonStyle(.borderedProminent)
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .padding(.horizontal, 20)
            .padding(.top, 12)
        }
    }

    @ViewBuilder
    private var modeSubheading: some View {
        if let mode = privacy.status?.mode {
            Text("Mode: \(mode) · read-only in v1")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            Text("Loading…")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var content: some View {
        if let err = privacy.lastError, privacy.apps.isEmpty {
            errorState(err)
        } else if privacy.apps.isEmpty {
            emptyState
        } else {
            List(privacy.apps) { app in
                PrivacyAppRow(app: app) { excluded in
                    Task { await privacy.toggleExclude(bundleId: app.bundleId, excluded: excluded) }
                }
            }
            .listStyle(.plain)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            if privacy.isLoading {
                ProgressView()
                    .controlSize(.large)
                Text("Loading installed apps…")
                    .foregroundStyle(.secondary)
            } else {
                Image(systemName: "app.dashed")
                    .font(.system(size: 36))
                    .foregroundStyle(.secondary)
                Text("No apps found")
                    .font(.headline)
                Text("Try refreshing the list.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    private func errorState(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 28))
                .foregroundStyle(.orange)
            Text("Couldn't load app list")
                .font(.headline)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button("Retry") {
                Task { await privacy.refreshApps() }
            }
            .buttonStyle(.borderedProminent)
            .disabled(privacy.isLoading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    private func initialLoad() async {
        async let appsLoad: Void = privacy.refreshApps()
        async let statusLoad: Void = privacy.refreshStatus()
        _ = await (appsLoad, statusLoad)
    }
}
