import SwiftUI

// U5 — the Library screen (design 343–377): a card grid over the recordings
// index with real thumbnails, filter chips, a header search pill + New-recording
// pill, and the empty / error / zero-match states the prototype omits. Replaced
// the retired `RecordingsListView` in the shell's Library route.
//
// The New-recording pill fires `onNewRecording` (U6's in-window sheet); the
// search pill fires `onOpenSearch` (U10's Recall palette, KTD-13); a card click
// fires `onOpenTimeline` (U9's day view).
struct LibraryView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var auth: CloudAuthController
    @EnvironmentObject private var store: StoreController
    @EnvironmentObject private var privacy: PrivacyController
    @Environment(\.openWindow) private var openWindow

    /// Present the New-recording sheet (U6). U5 wires this to the existing start
    /// path via MainWindow; the closure keeps LibraryView independent of the
    /// sheet that lands in U6.
    var onNewRecording: () -> Void
    /// U12: present the upgrade prompt when a lapsed user taps the gated
    /// New-recording control. Owned by MainWindow (like `onNewRecording`) so the
    /// sheet layers over the whole shell.
    var onUpgradePrompt: () -> Void
    /// U10: the header search pill opens the Recall palette (KTD-13).
    var onOpenSearch: () -> Void
    /// U9: a card click lands on the Day timeline seeked to the recording
    /// (day, wall-clock ms). Inspect stays reachable from the context menu.
    var onOpenTimeline: (Date, Int?) -> Void

    @State private var selectedChip: LibraryChip = .all
    // One frame resolver + thumbnail cache shared across every card (not one per
    // card), mirroring SearchView's SCR-177 wiring.
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()
    // U10 — one `tasks.list` per recording, shared across every card. Feeds the
    // card title/summary fallback for locally-named recordings (no cloud
    // round-trip); a recording with no tasks store keeps its existing title.
    @StateObject private var journalTasks = JournalTasks()
    @State private var rowError: String?
    @State private var restarting = false

    private static let columns = Array(repeating: GridItem(.flexible(), spacing: 20), count: 3)

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            .task {
                // SCR-258 U10: reflect an in-progress / paused encrypt migration so
                // the banner shows live progress (settings/containerEnabled are
                // already loaded at app launch).
                await privacy.refreshEncryptStatus()
            }
            .alert("Can't open recording", isPresented: rowErrorBinding) {
                Button("OK") { rowError = nil }
            } message: {
                Text(rowError ?? "")
            }
            // SCR-259: re-probe the daemon when the Library appears while the
            // "running without the background helper" advisory is showing, so
            // navigating back here after the daemon rebinds `api.sock` (the
            // launch-time swap window) clears the banner. RecordingsIndex's
            // app-activation observer covers window refocus; this covers in-app
            // navigation, which posts no activation notification. Gated so the
            // healthy path doesn't re-list on every appearance.
            .task {
                if index.usingCLIFallback { await index.refresh() }
            }
    }

    @ViewBuilder
    private var content: some View {
        if index.isLoading && index.recordings.isEmpty {
            loadingState
        } else if index.lastError != nil {
            errorState
        } else if !index.storeState.isMounted {
            // SCR-258 U10 (KTD-20, AE8): a locked / absent / key-missing store is a
            // FIRST-CLASS state, branched BEFORE the isEmpty check so a sealed store
            // never falls through to the "Nothing recorded yet" welcome screen.
            storeStateView
        } else if index.recordings.isEmpty {
            emptyState
        } else {
            populated
        }
    }

    private var storeStateView: some View {
        StoreStateView(
            storeState: index.storeState,
            onUnlock: { store.unlock() },
            onRetry: { Task { await index.refresh() } },
            onSetup: { store.initializeStore() },
            isBusy: store.phase != .idle
        )
    }

    // MARK: - Populated grid

    private var populated: some View {
        VStack(alignment: .leading, spacing: 0) {
            if index.usingCLIFallback { fallbackBanner }
            // U12: reassure a lapsed user that gating recording/search does NOT
            // touch their already-captured local recordings (R8), so gated never
            // reads as data loss. Only shown while actually gated.
            if auth.isGatedForLapse { recordingsSafeBanner }
            // SCR-258 U10: the one-shot encrypt-migration offer / progress banner.
            if showsMigrationBanner { migrationBanner }
            header
                .padding(.bottom, 22)
            chipRow
                .padding(.bottom, 24)
            grid
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 28)
    }

    private var visibleRecordings: [RecordingSummary] {
        LibraryGrid.visible(index.recordings, chip: selectedChip)
    }

    @ViewBuilder
    private var grid: some View {
        if visibleRecordings.isEmpty {
            zeroMatchNote
        } else {
            ScrollView {
                LazyVGrid(columns: Self.columns, alignment: .leading, spacing: 20) {
                    ForEach(visibleRecordings, id: \.stableID) { rec in
                        LibraryCard(
                            recording: rec,
                            frameIndex: frameIndex,
                            thumbnailLoader: thumbnailLoader,
                            tasks: journalTasks.tasks(for: rec),
                            onOpen: { open(rec) },
                            onInspect: { openInspect(rec) },
                            onReview: { openWindow(id: ReviewWindowID, value: rec.name) },
                            onRenamed: { Task { await index.refresh() } }
                        )
                        .task(id: rec.stableID) { await journalTasks.resolve(rec) }
                    }
                }
                .padding(.bottom, 8)
            }
            .scrollContentBackground(.hidden)
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .center) {
            Text("Library")
                .font(SCTypography.screenHeading)
                .foregroundStyle(Color.scInk)
            Spacer()
            HStack(spacing: 12) {
                searchPill
                newRecordingButton
            }
        }
    }

    private var searchPill: some View {
        Button {
            onOpenSearch()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12))
                Text("Search any moment")
                    .font(SCTypography.sans(size: 13))
                Spacer(minLength: 8)
                Text("⌘⇧F")
                    .font(SCTypography.mono(size: 10.5))
                    .foregroundStyle(Color.scInkFaint)
            }
            .foregroundStyle(Color.scInkMuted)
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .frame(width: 250)
            .background(Color.scCanvas, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("Search your recordings")
    }

    /// New-recording CTA — shown only when idle. During any active capture the
    /// recording banner (pinned above the detail area) is the single control
    /// surface for both the live status and Stop, so the header does not duplicate
    /// a Stop button (or the redundant `.starting` / `.stopping` progress labels)
    /// here.
    @ViewBuilder
    private var newRecordingButton: some View {
        if !recorder.state.isRecording {
            newRecordingPill
        }
    }

    /// The New-recording CTA — the normal filled teal pill, or, for a lapsed /
    /// not-entitled user (U12), a distinct gated pill whose press opens the
    /// upgrade prompt instead of the New-recording sheet. Never a silent no-op:
    /// the gated variant carries a lock glyph, muted styling, and an
    /// accessibility label + hint stating the subscription requirement.
    @ViewBuilder
    private var newRecordingPill: some View {
        if auth.isGatedForLapse {
            GatedRecordingPill(action: onUpgradePrompt)
        } else {
            LibraryPill(title: "New recording", filled: true, tint: .scTeal) {
                onNewRecording()
            }
        }
    }

    // MARK: - Chips

    private var chipRow: some View {
        HStack(spacing: 8) {
            ForEach(LibraryChip.allCases) { chip in
                chipButton(chip)
            }
        }
    }

    private func chipButton(_ chip: LibraryChip) -> some View {
        let active = chip == selectedChip
        return Button {
            selectedChip = chip
        } label: {
            Text(chip.label)
                .font(SCTypography.sans(size: 12.5, weight: active ? .medium : .regular))
                .foregroundStyle(active ? Color.scCanvas : Color.scInkSecondary)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(active ? Color.scInk : Color.clear, in: Capsule())
                .overlay(Capsule().strokeBorder(active ? Color.scInk : Color.scBorderWarm, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? [.isSelected] : [])
    }

    // MARK: - States

    private var fallbackBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "bolt.horizontal.circle")
                .foregroundStyle(Color.scInkSecondary)
            Text("Running without the background helper — showing recordings from disk.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color.scAdvisorySurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .padding(.bottom, 18)
    }

    // MARK: - Encrypt-migration banner (SCR-258 U10, KTD-18)

    private var showsMigrationBanner: Bool {
        VaultMigrationPolicy.shouldShowBanner(
            containerEnabled: privacy.containerEnabled == true,
            storeEncrypted: privacy.storeEncrypted == true,
            recordingsPresent: !index.recordings.isEmpty,
            state: privacy.encryptState,
            dismissed: privacy.encryptBannerDismissed
        )
    }

    @ViewBuilder
    private var migrationBanner: some View {
        let status = VaultMigrationPolicy.statusLine(for: privacy.encryptState)
        let isActive: Bool = {
            switch privacy.encryptState {
            case .migrating, .paused: return true
            default: return false
            }
        }()
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "lock.rectangle.stack")
                .foregroundStyle(Color.scTeal)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(VaultMigrationPolicy.bannerTitle)
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(VaultMigrationPolicy.bannerBody)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let status {
                    Text(status)
                        .font(SCTypography.sans(size: 12, weight: .medium))
                        .foregroundStyle(Color.scInkMuted)
                        .padding(.top, 2)
                }
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 6) {
                if isActive {
                    ProgressView().controlSize(.small)
                } else {
                    Button("Encrypt now") { Task { await privacy.startEncryption() } }
                        .buttonStyle(.borderedProminent)
                        .tint(Color.scTeal)
                    Button("Not now") { privacy.dismissEncryptBanner() }
                        .buttonStyle(.plain)
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkSecondary)
                }
            }
        }
        .padding(14)
        .background(Color.scAdvisorySurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .padding(.bottom, 18)
        .accessibilityElement(children: .combine)
    }

    /// U12 reassurance affordance: a lapsed user keeps browse + export of their
    /// own local recordings (R8). Reuses the advisory (not error) surface so it
    /// reads as calm "your data is safe", never as a failure.
    private var recordingsSafeBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "internaldrive")
                .foregroundStyle(Color.scInkSecondary)
            Text("Your recordings are safe on this Mac — browse and export them anytime. Recording and search need an active subscription.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
            Spacer(minLength: 0)
            Button("Subscribe") { onUpgradePrompt() }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12.5, weight: .semibold))
                .foregroundStyle(Color.scTeal)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(Color.scAdvisorySurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .padding(.bottom, 18)
        .accessibilityElement(children: .combine)
    }

    private var zeroMatchNote: some View {
        VStack(spacing: SCMetrics.space3) {
            Text("No \(selectedChip.label.lowercased()) recordings")
                .font(SCTypography.sans(size: 14, weight: .semibold))
                .foregroundStyle(Color.scInkSecondary)
            Button("Show all") { selectedChip = .all }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scTeal)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    private var emptyState: some View {
        VStack(spacing: SCMetrics.space4) {
            ShellLogoMark(size: 44)
            Text("Nothing recorded yet")
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
            Text("Start a recording and it will land here, on this Mac.")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340)
            newRecordingPill
                .padding(.top, SCMetrics.space2)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    @ViewBuilder
    private var errorState: some View {
        if index.lastErrorKind == .staleDaemon {
            staleDaemonError
        } else {
            genericError
        }
    }

    private var staleDaemonError: some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: "arrow.triangle.2.circlepath.circle")
                .font(.system(size: 34))
                .foregroundStyle(Color.scAmberText)
            Text("The background helper needs a restart")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text("ScreenCap updated but the helper is still running the old version. Restarting it reloads the recordings.")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            HStack(spacing: SCMetrics.space2) {
                Button(restarting ? "Restarting…" : "Restart helper") { restartHelper() }
                    .disabled(restarting)
                Button("Retry") { Task { await index.refresh() } }
                    .disabled(index.isLoading)
            }
            .padding(.top, SCMetrics.space1)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    private var genericError: some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 34))
                .foregroundStyle(Color.scErrorFg)
            Text("Couldn't load recordings")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(index.lastError ?? "")
                .font(SCTypography.metaMono)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            HStack(spacing: SCMetrics.space2) {
                Button("Retry") { Task { await index.refresh() } }
                    .disabled(index.isLoading)
                Button("Dismiss") { index.clearError() }
            }
            .padding(.top, SCMetrics.space1)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    private var loadingState: some View {
        VStack(spacing: SCMetrics.space3) {
            ProgressView().controlSize(.large)
            Text("Loading recordings…")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Actions

    /// Card tap → Day timeline seeked to the recording (U9). Recordings whose
    /// start day can't be derived fall back to the Inspect window. Stubs
    /// (uploaded, local media deleted) route to the timeline too — it renders
    /// their span with the honest evicted-media placeholder.
    private func open(_ rec: RecordingSummary) {
        guard let day = rec.startedDay else {
            openInspect(rec)
            return
        }
        onOpenTimeline(day, rec.startedAt.map { Int($0 * 1000) })
    }

    /// The read-only Inspect window (context menu; KTD-4). A stub recording
    /// surfaces the friendly download message rather than an empty window.
    private func openInspect(_ rec: RecordingSummary) {
        switch InspectRouting.decide(
            recording: rec.name, anchorMs: nil, isStub: rec.isStub,
            isEncrypted: rec.cloudE2EE == true
        ) {
        case .unavailable(let message):
            rowError = message
        case .open(let recording, _):
            InspectWindowOpener.shared.pendingSeekMs[recording] = nil
            openWindow(id: InspectWindowID, value: recording)
        }
    }

    private func restartHelper() {
        restarting = true
        Task {
            await DaemonInstallController.restartStaleDaemonIfNeeded()
            await index.refresh()
            restarting = false
        }
    }

    private var rowErrorBinding: Binding<Bool> {
        Binding(get: { rowError != nil }, set: { if !$0 { rowError = nil } })
    }
}

/// The design's pill buttons (New recording 352, Stop, empty-state CTA). A filled
/// teal/rust pill with a leading dot, or a flat progress label when `action` is
/// nil (the non-actionable `.starting` / `.stopping` states).
struct LibraryPill: View {
    let title: String
    var filled: Bool = true
    var tint: Color = .scTeal
    let action: (() -> Void)?

    @State private var hovering = false

    var body: some View {
        Group {
            if let action {
                Button(action: action) { label }
                    .buttonStyle(.plain)
                    .onHover { hovering = $0 }
            } else {
                label
            }
        }
    }

    private var label: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color.scCanvas)
                .frame(width: 8, height: 8)
            Text(title)
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
        }
        .foregroundStyle(Color.scCanvas)
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
        .background(hovering ? tint.opacity(0.88) : tint, in: Capsule())
        .contentShape(Capsule())
    }
}

/// The gated New-recording pill for a lapsed / not-entitled user (U12). Visually
/// distinct from the live teal `LibraryPill` — a muted outline pill with a lock
/// glyph — so the gated state reads at a glance rather than looking like a dead
/// button. It is NOT `.disabled`: pressing it opens the upgrade prompt (never a
/// silent no-op), so the a11y traits stay `.isButton`. The label + hint state the
/// subscription requirement for VoiceOver.
struct GatedRecordingPill: View {
    let action: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: "lock.fill")
                    .font(.system(size: 11, weight: .semibold))
                Text("New recording")
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
            }
            .foregroundStyle(Color.scInkSecondary)
            .padding(.horizontal, 20)
            .padding(.vertical, 10)
            .background(hovering ? Color.scFillSubtle : Color.clear, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel("New recording — subscription required")
        .accessibilityHint("Recording needs an active subscription. Opens the upgrade options.")
    }
}
