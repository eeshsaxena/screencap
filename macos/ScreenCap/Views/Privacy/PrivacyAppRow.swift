import AppKit
import SwiftUI

/// One row of the Privacy pane app list. Renders icon + display name +
/// bundle id + badge + toggle. Toggle behavior derives from
/// `PrivacyBadgeStyle.derive(for:)` so the rendering layer never re-encodes
/// the matrix-resolution priority order.
struct PrivacyAppRow: View {
    let app: InstalledApp
    let onToggleExclude: (Bool) -> Void

    @State private var localExcluded: Bool

    init(app: InstalledApp, onToggleExclude: @escaping (Bool) -> Void) {
        self.app = app
        self.onToggleExclude = onToggleExclude
        // Seed the local toggle from the derived state so the user sees an
        // optimistic update on tap; the next `refreshApps` overwrites this
        // from disk truth.
        _localExcluded = State(initialValue: PrivacyBadgeStyle.derive(for: app).toggleOn)
    }

    var body: some View {
        let style = PrivacyBadgeStyle.derive(for: app)
        HStack(spacing: 12) {
            icon
            VStack(alignment: .leading, spacing: 2) {
                Text(app.displayName)
                    .font(.body)
                Text(app.bundleId)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            badge(style)
            Toggle("", isOn: Binding(
                get: { localExcluded },
                set: { newValue in
                    localExcluded = newValue
                    onToggleExclude(newValue)
                }
            ))
            .labelsHidden()
            .toggleStyle(.switch)
            .disabled(style.toggleDisabled)
            .help(style.toggleDisabled ? (style.tooltip ?? "") : "")
        }
        .padding(.vertical, 6)
        .onChange(of: app) { newApp in
            // Disk refreshed under us — reseed the optimistic local toggle.
            localExcluded = PrivacyBadgeStyle.derive(for: newApp).toggleOn
        }
    }

    private var icon: some View {
        Group {
            if let nsImage = Self.loadIcon(for: app.path) {
                Image(nsImage: nsImage)
                    .resizable()
                    .interpolation(.high)
            } else {
                Image(systemName: "app.dashed")
                    .resizable()
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 28, height: 28)
    }

    private func badge(_ style: PrivacyBadgeStyle) -> some View {
        Text(style.text)
            .font(.caption)
            .foregroundStyle(style.color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(style.color.opacity(0.12), in: Capsule())
            .help(style.tooltip ?? "")
    }

    /// Loads `.app/Contents/Resources/<icon>` via NSWorkspace. Returns nil for
    /// paths that aren't real bundles (defensive — `apps --json` is supposed
    /// to filter, but a stale bundle on disk shouldn't crash the row).
    private static func loadIcon(for path: String) -> NSImage? {
        guard !path.isEmpty else { return nil }
        let image = NSWorkspace.shared.icon(forFile: path)
        return image
    }
}
