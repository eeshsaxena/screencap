import SwiftUI

/// The Privacy sidebar pane (SCR-17 / U3). Header with the configured mode
/// (read-only in v1) + manual refresh button + scrollable list of installed
/// apps with per-row state badges and exclude toggles.
///
/// Refreshes on appear; live config-watcher is deferred — friend-trial
/// feedback decides whether to add one.
struct PrivacyPaneView: View {
    @EnvironmentObject private var privacy: PrivacyController

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
        }
        .task { await initialLoad() }
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
