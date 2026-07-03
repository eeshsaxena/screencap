import SwiftUI

/// U13 — the prototype App rules pane (design 518–549): intro copy, the
/// default-for-new-apps banner (Record active; Mask/Block stubbed SCR-225),
/// and per-app rows — 30px initials tile, name, mono note, and the tri-state
/// Record/Mask/Block segmented control. Writable where the backend has
/// vocabulary (`allow_apps` / `exclude_apps` via the CLI settings layer, R8),
/// locked where it doesn't (matrix-immutable rows; the Mask override is
/// SCR-225). Row semantics derive from `AppRuleSegmentPolicy`.
struct AppRulesView: View {
    @EnvironmentObject private var privacy: PrivacyController

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("App rules")
                .font(SCTypography.paneHeading)
                .foregroundStyle(Color.scInk)
                .padding(.bottom, 6)
            // The design's "in ambient and focused recording alike" clause is
            // SCR-214 vocabulary — dropped until ambient recording exists.
            Text("What happens when each app is on screen. Blocked apps are cut from the video.")
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInkMuted)
                .padding(.bottom, 18)

            defaultForNewAppsBanner
                .padding(.bottom, 12)

            content
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 30)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color.scCanvas)
        .task {
            await privacy.refreshApps()
        }
    }

    // MARK: - Default-for-new-apps banner (design 522–529; stub SCR-225)

    /// "Record" reflects the real matrix default for unlisted apps; choosing a
    /// different default is per-app-override territory (SCR-225), so the other
    /// segments are stubs.
    private var defaultForNewAppsBanner: some View {
        HStack(spacing: 12) {
            (
                // Text-concatenation styling must return `Text`, and the
                // Text-returning foregroundStyle overload is macOS 14+ — the
                // deployment target is 13, so these two use foregroundColor.
                Text("Default for new apps").font(SCTypography.sans(size: 13, weight: .semibold)).foregroundColor(.scInk)
                + Text(" — anything not listed below").font(SCTypography.sans(size: 13)).foregroundColor(.scInkSecondary)
            )
            Spacer(minLength: 8)
            HStack(spacing: 0) {
                segmentLabel("Record", state: .selected(.record), enabled: false)
                segmentLabel("Mask", state: .idle, enabled: false)
                segmentLabel("Block", state: .idle, enabled: false)
            }
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .clipShape(Capsule())
            .help(AppRuleSegmentPolicy.maskStubHelp)  // Stub: SCR-225 default-for-new-apps rule
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .frame(maxWidth: 760, alignment: .leading)
        .background(Color.scCanvas, in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
    }

    // MARK: - Rows

    @ViewBuilder
    private var content: some View {
        if let err = privacy.lastError, privacy.apps.isEmpty {
            errorState(err)
        } else if privacy.apps.isEmpty {
            emptyState
        } else {
            ScrollView {
                LazyVStack(spacing: 0) {
                    privateWindowsStubRow
                    Rectangle().fill(Color.scFillSubtle).frame(height: 1)
                    ForEach(sortedApps) { app in
                        AppRuleRow(app: app) { transition in
                            apply(transition, to: app)
                        }
                        Rectangle().fill(Color.scFillSubtle).frame(height: 1)
                    }
                }
                .frame(maxWidth: 760, alignment: .leading)
            }
        }
    }

    /// The design's ordering: locked always-blocked rows first, then the
    /// user's own blocks, then masked, then everything else — alphabetical
    /// within each group.
    private var sortedApps: [InstalledApp] {
        func rank(_ app: InstalledApp) -> Int {
            let policy = AppRuleSegmentPolicy.derive(for: app)
            if app.isMatrixExclude { return 0 }
            if policy.selection == .block { return 1 }
            if policy.selection == .mask { return 2 }
            return 3
        }
        return privacy.apps.sorted {
            let (ra, rb) = (rank($0), rank($1))
            if ra != rb { return ra < rb }
            return $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
        }
    }

    private func apply(_ transition: AppRuleSegmentPolicy.Transition, to app: InstalledApp) {
        Task {
            switch transition {
            case .excludeAdd:
                await privacy.toggleExclude(bundleId: app.bundleId, excluded: true)
            case .excludeRemove:
                await privacy.toggleExclude(bundleId: app.bundleId, excluded: false)
            case .allowAdd:
                await privacy.toggleAllow(bundleId: app.bundleId, allowed: true)
            }
        }
    }

    /// The design's "Safari — private windows" row. Private-window detection
    /// does not exist yet (SCR-224), so the row renders with no selected
    /// segment and honest future-tense copy — a Block-selected segment would
    /// claim a protection the recorder can't deliver (R7).
    private var privateWindowsStubRow: some View {
        HStack(spacing: 12) {
            AppInitialsTile(text: "Sa", color: Color.tileColor(for: "safari-private-windows"), size: 30)
            VStack(alignment: .leading, spacing: 2) {
                Text("Safari — private windows")
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text("auto-pause is on the way")
                    .font(SCTypography.metaMonoSmall)
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            HStack(spacing: 0) {
                segmentLabel("Record", state: .idle, enabled: false)
                segmentLabel("Mask", state: .idle, enabled: false)
                segmentLabel("Block", state: .idle, enabled: false)
            }
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .clipShape(Capsule())
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 11)
        .opacity(0.7)
        .help("Coming soon — SCR-224")  // Stub: SCR-224 private-window detection / auto-pause
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            if privacy.isLoading {
                ProgressView().controlSize(.large)
                Text("Loading installed apps…")
                    .font(SCTypography.sans(size: 13))
                    .foregroundStyle(Color.scInkMuted)
            } else {
                Text("No apps found")
                    .font(SCTypography.sectionTitle)
                    .foregroundStyle(Color.scInk)
                Button("Retry") { Task { await privacy.refreshApps() } }
                    .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    private func errorState(_ message: String) -> some View {
        VStack(spacing: 12) {
            Text("Couldn't load app list")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(message)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkMuted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button("Retry") { Task { await privacy.refreshApps() } }
                .buttonStyle(.borderedProminent)
                .disabled(privacy.isLoading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }
}

/// One app row (design 530–547).
private struct AppRuleRow: View {
    let app: InstalledApp
    let onTransition: (AppRuleSegmentPolicy.Transition) -> Void

    var body: some View {
        let policy = AppRuleSegmentPolicy.derive(for: app)
        HStack(spacing: 12) {
            AppInitialsTile(
                text: AppInitialsTile.initials(for: app.displayName),
                color: Color.tileColor(for: app.bundleId),
                size: 30
            )
            VStack(alignment: .leading, spacing: 2) {
                Text(app.displayName)
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(policy.note)
                    .font(SCTypography.metaMonoSmall)
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            segmentedControl(policy)
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 11)
        .help(policy.lockedReason ?? "")
    }

    private func segmentedControl(_ policy: AppRuleSegmentPolicy) -> some View {
        HStack(spacing: 0) {
            segmentButton(
                "Record",
                state: policy.selection == .record ? .selected(.record) : .idle,
                enabled: policy.recordEnabled && policy.selection != .record,
                help: nil
            ) {
                fire(.record)
            }
            segmentButton(
                "Mask",
                state: policy.selection == .mask ? .selected(.mask) : .idle,
                // The Mask segment never accepts interaction in v1: it either
                // shows the matrix's own (real) state or stubs the SCR-225
                // per-app override.
                enabled: false,
                help: policy.selection == .mask ? nil : AppRuleSegmentPolicy.maskStubHelp
            ) {}
            segmentButton(
                "Block",
                state: policy.selection == .block ? .selected(.block) : .idle,
                enabled: policy.blockEnabled && policy.selection != .block,
                help: nil
            ) {
                fire(.block)
            }
        }
        .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
        .clipShape(Capsule())
    }

    private func fire(_ segment: AppRuleSegmentPolicy.Segment) {
        guard let transition = AppRuleSegmentPolicy.transition(for: app, tapping: segment) else {
            return
        }
        onTransition(transition)
    }

    private func segmentButton(
        _ label: String,
        state: SegmentVisualState,
        enabled: Bool,
        help: String?,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            segmentLabel(label, state: state, enabled: enabled)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .help(help ?? "")
    }
}

/// The per-state segment colors (logic 713–736): record teal, mask amber (dark
/// ink text), block rust.
enum SegmentVisualState: Equatable {
    case selected(AppRuleSegmentPolicy.Segment)
    case idle
}

/// Shared segment chrome for the rows and the default-for-new-apps banner.
@ViewBuilder
func segmentLabel(_ label: String, state: SegmentVisualState, enabled: Bool) -> some View {
    let colors: (bg: Color, fg: Color) = {
        switch state {
        case .selected(.record): return (.scTeal, .scCanvas)
        case .selected(.mask): return (.scAmber, .scInk)
        case .selected(.block): return (.scRust, .scPaper)
        case .idle: return (.clear, .scInkSecondary)
        }
    }()
    Text(label)
        .font(SCTypography.sans(size: 12, weight: state == .idle ? .regular : .semibold))
        .foregroundStyle(colors.fg)
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(colors.bg)
        .contentShape(Rectangle())
        .opacity(enabled || state != .idle ? 1 : 0.75)
}
