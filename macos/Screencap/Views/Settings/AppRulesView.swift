import SwiftUI

/// U13 — the prototype App rules pane (design 518–549): intro copy, the
/// default-action banner, and per-app rows — 30px initials tile, name, mono
/// note, and the tri-state Record/Mask/Block segmented control. All three
/// segments write since SCR-225 (`allow_apps` / `mask_apps` / `exclude_apps`
/// via the CLI settings layer, R8), as does the banner (`default_action`). The
/// only non-writable row left is the SCR-224 private-windows stub. Row
/// semantics derive from `AppRuleSegmentPolicy`.
struct AppRulesView: View {
    @EnvironmentObject private var privacy: PrivacyController

    /// Bundle ids with a rule write in flight, mapped to the segment the user
    /// tapped. Drives the optimistic selection: the tapped segment renders
    /// selected immediately (the CLI write + `apps --json` refresh takes
    /// ~1–2s), the row locks against further taps, and disk truth replaces the
    /// optimistic state when the refresh lands — so a failed write visibly
    /// snaps back instead of lying.
    @State private var pendingSegments: [String: AppRuleSegmentPolicy.Segment] = [:]

    /// The app awaiting the SCR-235 confirmation dialog (Record tapped on a
    /// confirmation-required row). Cancel writes nothing; Confirm issues the
    /// CLI write with the confirm flag.
    @State private var confirmingApp: InstalledApp?
    /// Locks the banner for the round trip of a default-action write, so a
    /// rapid second tap can't race the first (mirrors `pendingSegments` for
    /// rows, but the banner is a single control so one flag suffices).
    @State private var defaultWritePending = false

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
        .confirmationDialog(
            confirmingApp.map { "Allow “\($0.displayName)”?" } ?? "",
            isPresented: Binding(
                get: { confirmingApp != nil },
                set: { if !$0 { confirmingApp = nil } }
            ),
            titleVisibility: .visible,
            presenting: confirmingApp
        ) { app in
            Button("Allow and record") { runConfirmedAllow(app) }
            Button("Cancel", role: .cancel) {}
        } message: { app in
            Text(AppRuleSegmentPolicy.confirmationMessage(for: app))
        }
    }

    /// Fire the confirmed-allow write after the user accepted the dialog.
    /// Mirrors `apply(_:to:tapped:)`'s optimistic pending discipline.
    private func runConfirmedAllow(_ app: InstalledApp) {
        guard pendingSegments[app.bundleId] == nil else { return }
        pendingSegments[app.bundleId] = .record
        Task {
            await privacy.confirmAllow(bundleId: app.bundleId)
            pendingSegments.removeValue(forKey: app.bundleId)
        }
    }

    // MARK: - Default-action banner (design 522–529; live since SCR-225)

    /// The blanket floor for apps with no explicit rule. Deliberately NOT
    /// "anything not listed below" — every installed app is listed below, so
    /// that phrasing described a partial list that does not exist. What the
    /// control actually governs is any app the user has not given a rule.
    ///
    /// Its Record differs from a row's Record: this one means "let the app's
    /// category decide", while a row's Record is an explicit per-app allow that
    /// overrides the category. Stricter values here can only tighten.
    private var defaultForNewAppsBanner: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                (
                    // Text-concatenation styling must return `Text`, and the
                    // Text-returning foregroundStyle overload is macOS 14+ — the
                    // deployment target is 13, so these two use foregroundColor.
                    Text("Default for apps you haven't set").font(SCTypography.sans(size: 13, weight: .semibold)).foregroundColor(.scInk)
                    + Text(" — applies to every app below without its own rule").font(SCTypography.sans(size: 13)).foregroundColor(.scInkSecondary)
                )
                Text(defaultActionCaption)
                    .font(SCTypography.metaMonoSmall)
                    .foregroundStyle(Color.scInkMuted)
            }
            Spacer(minLength: 8)
            HStack(spacing: 0) {
                defaultSegment("Record", value: "allow", segment: .record)
                defaultSegment("Mask", value: "mask_window", segment: .mask)
                defaultSegment("Block", value: "exclude", segment: .block)
            }
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .clipShape(Capsule())
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

    /// Names the consequence of the current default in plain terms. The strict
    /// values have a wide blast radius — for most libraries "no rule" is nearly
    /// every app — so the banner says so rather than leaving it to the label.
    private var defaultActionCaption: String {
        switch privacy.defaultAction {
        case "exclude":
            return "nothing is recorded unless you set it to Record"
        case "mask_window":
            return "windows are masked unless you set them to Record"
        default:
            return "each app's category decides"
        }
    }

    /// One banner segment. Selected state comes from the controller's published
    /// default, so a failed write reverting the value also reverts the control.
    @ViewBuilder
    private func defaultSegment(
        _ label: String,
        value: String,
        segment: AppRuleSegmentPolicy.Segment
    ) -> some View {
        let selected = privacy.defaultAction == value
        Button {
            guard !selected, !defaultWritePending else { return }
            defaultWritePending = true
            Task {
                await privacy.setDefaultAction(value)
                defaultWritePending = false
            }
        } label: {
            segmentLabel(
                label,
                state: selected ? .selected(segment) : .idle,
                enabled: !selected && !defaultWritePending
            )
        }
        .buttonStyle(.plain)
        .disabled(selected || defaultWritePending)
    }

    // MARK: - Rows

    @ViewBuilder
    private var content: some View {
        if let err = privacy.lastError, privacy.apps.isEmpty {
            errorState(err)
        } else if privacy.apps.isEmpty {
            emptyState
        } else {
            VStack(alignment: .leading, spacing: 0) {
                // A rule write failed while the list is populated — surface it
                // inline (the toggle's optimistic state has already snapped
                // back to disk truth) rather than failing silently.
                if let err = privacy.lastError {
                    Text("Couldn't save the last change: \(err)")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scRust)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 8)
                }
                ScrollView {
                    LazyVStack(spacing: 0) {
                        privateWindowsStubRow
                        Rectangle().fill(Color.scFillSubtle).frame(height: 1)
                        ForEach(Self.stableOrder(privacy.apps)) { app in
                            AppRuleRow(
                                app: app,
                                pending: pendingSegments[app.bundleId]
                            ) { segment, transition in
                                apply(transition, to: app, tapped: segment)
                            }
                            Rectangle().fill(Color.scFillSubtle).frame(height: 1)
                        }
                    }
                    .frame(maxWidth: 760, alignment: .leading)
                }
            }
        }
    }

    /// Row order is deliberately STABLE under rule changes: only the
    /// sensitive confirmation-required rows group at the top (the class is
    /// immutable, so toggling a rule never moves a row), and everything else
    /// is alphabetical regardless of its current rule. Ranking rows by their
    /// user-toggleable state made a just-toggled row jump groups
    /// mid-interaction, shifting every row under the cursor — the "clicked
    /// one app, changed another" failure the live QA caught.
    static func stableOrder(_ apps: [InstalledApp]) -> [InstalledApp] {
        apps.sorted {
            if $0.confirmationRequired != $1.confirmationRequired { return $0.confirmationRequired }
            return $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
        }
    }

    /// Fire the CLI transition with per-bundle optimistic pending state. The
    /// pending entry clears only after the controller's own `refreshApps`
    /// completes (success or failure), so the row is locked for exactly the
    /// window where a second tap could race the write.
    private func apply(
        _ transition: AppRuleSegmentPolicy.Transition,
        to app: InstalledApp,
        tapped segment: AppRuleSegmentPolicy.Segment
    ) {
        guard pendingSegments[app.bundleId] == nil else { return }
        // The confirmation-required unlock defers the write to the dialog:
        // no pending state yet, so Cancel leaves the row untouched.
        if transition == .allowConfirm {
            confirmingApp = app
            return
        }
        pendingSegments[app.bundleId] = segment
        Task {
            switch transition {
            case .excludeAdd:
                await privacy.toggleExclude(bundleId: app.bundleId, excluded: true)
            case .excludeRemove:
                await privacy.toggleExclude(bundleId: app.bundleId, excluded: false)
            case .allowAdd:
                await privacy.toggleAllow(bundleId: app.bundleId, allowed: true)
            case .allowConfirm:
                break  // handled above before pending state is set
            case .maskAdd:
                await privacy.toggleMask(bundleId: app.bundleId)
            }
            pendingSegments.removeValue(forKey: app.bundleId)
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
    /// The optimistic in-flight segment (nil when idle). While set, it renders
    /// as the selection and the whole control locks — the user's tap responds
    /// instantly instead of waiting out the CLI round-trip.
    let pending: AppRuleSegmentPolicy.Segment?
    let onTap: (AppRuleSegmentPolicy.Segment, AppRuleSegmentPolicy.Transition) -> Void

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
        // The optimistic pending segment overrides the disk-derived selection
        // while the write round-trips; the control locks so a second tap
        // can't race the in-flight write.
        let selection = pending ?? policy.selection
        let busy = pending != nil
        return HStack(spacing: 0) {
            segmentButton(
                "Record",
                state: selection == .record ? .selected(.record) : .idle,
                enabled: !busy && policy.recordEnabled && selection != .record,
                help: nil
            ) {
                fire(.record)
            }
            segmentButton(
                "Mask",
                state: selection == .mask ? .selected(.mask) : .idle,
                // Writable since SCR-225, but only where a Mask rule would
                // change the outcome — `maskEnabled` is false on rows that
                // already mask or that resolve to the stricter EXCLUDE.
                enabled: !busy && policy.maskEnabled,
                help: nil
            ) {
                fire(.mask)
            }
            segmentButton(
                "Block",
                state: selection == .block ? .selected(.block) : .idle,
                enabled: !busy && policy.blockEnabled && selection != .block,
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
        onTap(segment, transition)
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
