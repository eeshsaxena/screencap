import SwiftUI

/// The detail routes the shell can display (U4). `privacy` renders the
/// prototype Privacy settings pane (U12), `appRules` the App rules pane
/// (U13), and `intelligence` the Intelligence pane (U9 — model picker + the
/// per-task cloud-consent matrix). `timeline` has no sidebar row — it is
/// reached from Journal day links and recording cards (U9), optionally
/// carrying a wall-clock seek anchor.
enum ShellRoute: Hashable {
    case library
    case journal
    /// The conversational-recall Chat destination — the FIRST search-like sidebar
    /// route (KTD7). Distinct from Search, which is the `RecallPaletteView` overlay
    /// palette (NOT a sidebar destination). Chat and Search share the retrieval
    /// backend and Search's result components, not a destination pattern.
    case chat
    case timeline(day: Date, seekMs: Int?)
    case privacy
    case appRules
    case intelligence
}

/// A sidebar nav row's presentation contract — pure, so the routing / enablement /
/// stub rules are unit-testable without a running view (U4 test scenarios).
struct ShellNavItem: Identifiable, Hashable {
    /// Why a row is (un)available, which decides its enabled state + help text.
    enum Availability: Hashable {
        /// Routes now (its pane exists).
        case enabled
        /// A deferred capability (KTD-8): disabled with "Coming soon — SCR-NNN".
        case stub(ticket: String)
    }

    let id: String
    let label: String
    /// The route this row selects, or nil for a pure stub row with no destination.
    let route: ShellRoute?
    let availability: Availability

    var isEnabled: Bool { availability == .enabled }

    /// Help tooltip for a disabled row (nil when enabled).
    var helpText: String? {
        switch availability {
        case .enabled: return nil
        case .stub(let ticket): return "Coming soon — \(ticket)"
        }
    }
}

/// Pure sidebar model — the nav vocabulary and the footer storage math, factored
/// out of the view so U4's routing / stub / footer rules are directly assertable.
enum ShellSidebarModel {

    static let primaryNav: [ShellNavItem] = [
        ShellNavItem(id: "library", label: "Library", route: .library, availability: .enabled),
        ShellNavItem(id: "journal", label: "Journal", route: .journal, availability: .enabled),
        // Chat — the first search-like sidebar destination (KTD7). Ask about your
        // recorded history and get a grounded answer with the real moments as
        // sources.
        ShellNavItem(id: "chat", label: "Chat", route: .chat, availability: .enabled),
    ]

    /// The design's COLLECTIONS list is mock data (sample collection names,
    /// which the U14 sweep forbids), so Collections ships as a single honest stub
    /// row until SCR-222 — never the sample names.
    static let collections: [ShellNavItem] = [
        ShellNavItem(id: "collections", label: "Collections", route: nil,
                     availability: .stub(ticket: "SCR-222")),
    ]

    static let settingsNav: [ShellNavItem] = [
        ShellNavItem(id: "privacy", label: "Privacy", route: .privacy, availability: .enabled),
        ShellNavItem(id: "appRules", label: "App rules", route: .appRules, availability: .enabled),
        ShellNavItem(id: "intelligence", label: "Intelligence", route: .intelligence, availability: .enabled),
    ]

    /// The status-footer storage summary. `allLocal` gates the "all local ·"
    /// prefix (shown only when nothing has been uploaded — KTD-6 / R7).
    struct StorageFooter: Equatable {
        let byteText: String
        let allLocal: Bool
        var line: String { (allLocal ? "all local · " : "") + byteText + " on this Mac" }
    }

    static func storageFooter(_ recordings: [RecordingSummary]) -> StorageFooter {
        let bytes = recordings.reduce(0) { $0 + $1.sizeBytes }
        let anyUploaded = recordings.contains { $0.uploaded }
        return StorageFooter(byteText: formatStorage(bytes), allLocal: !anyUploaded)
    }

    /// SCR-239 (U11) — whether the standing "enable local intelligence" sidebar
    /// hint should show: only when the active provider is on-device, no
    /// downloadable model is installed yet, and the user hasn't dismissed it.
    /// A cloud provider (which already names tasks) or an installed model hides it.
    static func shouldShowLocalModelHint(
        provider: String?,
        downloadedInstalled: Bool,
        dismissed: Bool
    ) -> Bool {
        provider == "on-device" && !downloadedInstalled && !dismissed
    }

    /// Format a byte total the design's way ("4.2 GB"), stepping down to MB/KB for
    /// small libraries.
    static func formatStorage(_ bytes: Int) -> String {
        let b = Double(max(0, bytes))
        let gb = b / 1_073_741_824
        if gb >= 1 { return String(format: "%.1f GB", gb) }
        let mb = b / 1_048_576
        if mb >= 1 { return String(format: "%.0f MB", mb) }
        return String(format: "%.0f KB", b / 1024)
    }
}

/// The Screencap brand mark — two rounded corner brackets around a teal dot (the
/// inline SVG at design line 245), drawn as a `Path` so it scales crisply.
struct ShellLogoMark: View {
    var size: CGFloat = 22

    var body: some View {
        Canvas { ctx, canvasSize in
            let s = canvasSize.width / 48  // the SVG viewBox is 0 0 48 48
            func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x * s, y: y * s) }

            var topLeft = Path()
            topLeft.move(to: p(20, 6))
            topLeft.addLine(to: p(12, 6))
            topLeft.addQuadCurve(to: p(6, 12), control: p(6, 6))
            topLeft.addLine(to: p(6, 20))

            var bottomRight = Path()
            bottomRight.move(to: p(28, 42))
            bottomRight.addLine(to: p(36, 42))
            bottomRight.addQuadCurve(to: p(42, 36), control: p(42, 42))
            bottomRight.addLine(to: p(42, 28))

            let stroke = StrokeStyle(lineWidth: 4.5 * s, lineCap: .round)
            ctx.stroke(topLeft, with: .color(.scInk), style: stroke)
            ctx.stroke(bottomRight, with: .color(.scInk), style: stroke)

            let r = 8.5 * s
            ctx.fill(
                Path(ellipseIn: CGRect(x: 24 * s - r, y: 24 * s - r, width: 2 * r, height: 2 * r)),
                with: .color(.scTeal)
            )
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// The prototype sidebar (design lines 299–337): traffic-light inset, brand mark,
/// Library/Journal nav, a Collections stub, the Privacy/App-rules settings group,
/// and the status footer. Replaces the legacy four-pane `List` sidebar (U4).
struct ShellSidebarView: View {
    @Binding var route: ShellRoute
    let recordings: [RecordingSummary]
    var onReplayOnboarding: () -> Void

    /// Injected app-wide (ScreenCapApp) — drives the SCR-239 local-intelligence
    /// hint from the Intelligence read-back (provider + install state).
    @EnvironmentObject private var intelligence: IntelligenceController

    /// The persisted once-dismissed flag for the local-model hint (R7 — never
    /// re-prompt once dismissed).
    @State private var hintDismissed = HUDHintStore().hasDismissedLocalModelHint

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            windowControlsSlot
            brand
            navGroup(ShellSidebarModel.primaryNav)
            sectionHeader("COLLECTIONS")
            navGroup(ShellSidebarModel.collections)
            sectionHeader("SETTINGS")
            navGroup(ShellSidebarModel.settingsNav)
            Spacer(minLength: SCMetrics.space4)
            footer
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 18)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Color.scCanvas)
        .task { await intelligence.refresh() }
    }

    /// The standing, dismissible "enable local intelligence" hint (SCR-239 U11).
    /// Shown only when on-device is active and no model is installed; tapping it
    /// opens the Intelligence pane, the [x] dismisses it for good (R7).
    @ViewBuilder
    private var localModelHint: some View {
        let show = ShellSidebarModel.shouldShowLocalModelHint(
            provider: intelligence.settings?.provider,
            downloadedInstalled: intelligence.settings?.downloadedModelInstalled ?? false,
            dismissed: hintDismissed
        )
        if show {
            HStack(alignment: .top, spacing: 8) {
                Button { route = .intelligence } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Name your tasks on this Mac")
                            .font(SCTypography.sans(size: 12, weight: .semibold))
                            .foregroundStyle(Color.scInk)
                        Text("Download a local model — on-device, nothing leaves.")
                            .font(SCTypography.sans(size: 11))
                            .foregroundStyle(Color.scInkMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .buttonStyle(.plain)
                Spacer(minLength: 0)
                Button {
                    hintDismissed = true
                    HUDHintStore().markLocalModelHintDismissed()
                } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.scInkMuted)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Dismiss")
            }
            .padding(10)
            .background(Color.scTeal.opacity(0.08), in: RoundedRectangle(cornerRadius: SCMetrics.radiusInner))
        }
    }

    /// The design draws mock traffic-light dots here (300–304) — that is the
    /// prototype's fake window chrome. The window uses `.hiddenTitleBar`, so
    /// the REAL controls overlay this slot; reserve the dots row's height
    /// (12pt + 22pt gap) instead of drawing a second set (U14 fidelity pass).
    private var windowControlsSlot: some View {
        Color.clear
            .frame(height: 12)
            .padding(.bottom, 22)
    }

    private var brand: some View {
        HStack(spacing: 9) {
            ShellLogoMark(size: 22)
            Text("Screencap")
                .font(SCTypography.serifBrand)
                .foregroundStyle(Color.scInk)
        }
        .padding(.horizontal, 6)
        .padding(.bottom, SCMetrics.space6)
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(SCTypography.metaMonoSmall)
            .tracking(1.05)  // 0.1em at ~10.5pt
            .foregroundStyle(Color.scInkMuted)
            .padding(.horizontal, SCMetrics.space3)
            .padding(.top, SCMetrics.space6)
            .padding(.bottom, SCMetrics.space2)
            .accessibilityAddTraits(.isHeader)
    }

    private func navGroup(_ items: [ShellNavItem]) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(items) { item in
                navRow(item)
            }
        }
    }

    @ViewBuilder
    private func navRow(_ item: ShellNavItem) -> some View {
        let isActive = item.route.map { $0 == route } ?? false
        Button {
            if let dest = item.route { route = dest }
        } label: {
            HStack(spacing: 10) {
                dot(active: isActive, enabled: item.isEnabled)
                Text(item.label)
                    .font(SCTypography.sans(size: 13.5, weight: isActive ? .semibold : .regular))
                Spacer(minLength: 0)
            }
            .padding(.horizontal, SCMetrics.space3)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: SCMetrics.radiusInner)
                    .fill(isActive ? Color.scTeal.opacity(0.1) : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!item.isEnabled)
        .foregroundStyle(rowForeground(active: isActive, enabled: item.isEnabled))
        .help(item.helpText ?? "")
        .accessibilityHint(item.helpText ?? "")
    }

    private func dot(active: Bool, enabled: Bool) -> some View {
        Circle()
            .fill(active ? Color.scTeal : Color.clear)
            .frame(width: 7, height: 7)
            .overlay(
                Circle().stroke(
                    active ? Color.clear : Color.scInkFaint.opacity(enabled ? 0.6 : 0.35),
                    lineWidth: 1
                )
            )
    }

    private func rowForeground(active: Bool, enabled: Bool) -> Color {
        if !enabled { return .scInkFaint }
        return active ? .scInk : .scInkSecondary
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider().overlay(Color.scBorderWarm)
                .padding(.bottom, 6)
            localModelHint
            // MCP row — a stub until SCR-226 (no daemon verb reports connected MCP
            // clients yet), so it never shows a fabricated count.
            HStack(spacing: SCMetrics.space2) {
                Circle().fill(Color.scInkFaint).frame(width: 7, height: 7)
                Text("MCPs — coming soon")
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkSecondary)
            }
            .help("Coming soon — SCR-226")  // Stub: SCR-226 daemon verb for connected MCP clients
            Text(ShellSidebarModel.storageFooter(recordings).line)
                .font(SCTypography.mono(size: 11))
                .foregroundStyle(Color.scInkMuted)
            Button(action: onReplayOnboarding) {
                Text("Replay onboarding")
                    .font(SCTypography.sans(size: 12))
                    .underline()
                    .foregroundStyle(Color.scInkMuted)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, SCMetrics.space3)
        .padding(.top, SCMetrics.space4)
    }
}
