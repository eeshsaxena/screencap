import SwiftUI

// U3 — the Days screen: the default landing surface (R2). Day cards derive from
// day-clamped coverage (KTD-6), newest-first, each opening its day page. An
// always-present Today card (R15/AE6) carries live capture status. A day-level
// upload/review badge (R14) routes into the existing Review & upload window. No
// browsing surface shows a recording entity (R5): cards carry the day's task
// summary, never a recording title.
//
// Rescoped from the retired Journal screen. Carries over the Library surface's
// obligations: the sealed-vault branch precedes the empty state (KTD-20), the
// migration / lapse / stale-daemon banners survive, and every zero state is
// honest (never-recorded, ambient off) rather than a blank.
struct DaysView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var auth: CloudAuthController
    @EnvironmentObject private var store: StoreController
    @EnvironmentObject private var privacy: PrivacyController
    @Environment(\.openWindow) private var openWindow

    /// Present the New-recording sheet (re-homed onto the Days header from the
    /// retired Library header). Owned by MainWindow so it layers over the shell.
    var onNewRecording: () -> Void
    /// Present the upgrade prompt when a lapsed user taps the gated New-recording
    /// control (R8 reassurance path).
    var onUpgradePrompt: () -> Void
    /// The header search pill opens the Recall palette (KTD-13).
    var onOpenSearch: () -> Void
    /// A card click opens the day page (day, optional wall-clock seek anchor).
    var onOpenTimeline: (Date, Int?) -> Void

    // One frame resolver + thumbnail cache shared across every card.
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()
    // One `tasks.list` per recording, shared across every day card, for the
    // day-level task summaries (write-through cache preserved for curation paths).
    @StateObject private var dayTasks = DayTasks()
    @StateObject private var appChips = DayAppChips()
    // Owns its own AmbientController (mirroring AmbientRecordingSection) to drive
    // the Today card's live capture status — additive, no app-root plumbing.
    @StateObject private var ambient = AmbientController()

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            // Live Today-card status (KTD-12): recorder.state flips on recording
            // start/stop (driven by the daemon event stream); a light poll also
            // catches pause/resume, which don't move recorder.state.
            .task {
                await ambient.load()
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                    if Task.isCancelled { break }
                    await ambient.load()
                }
            }
            .task { await privacy.refreshEncryptStatus() }
            // Re-probe the daemon when Days appears while the CLI-fallback advisory
            // is showing, so navigating back after the daemon rebinds clears it.
            .task {
                if index.usingCLIFallback { await index.refresh() }
            }
            // Live-refresh the Today card on recording lifecycle edges (KTD-12):
            // a recording start/stop (ambient attach included) flips recorder.state.
            .onChange(of: recorder.state) { _ in
                Task { await ambient.load() }
            }
    }

    @ViewBuilder
    private var content: some View {
        if index.isLoading && index.recordings.isEmpty {
            loadingState
        } else if index.lastError != nil {
            errorState
        } else if !index.storeState.isMounted {
            // KTD-20 / AE8: a sealed / absent / key-missing store is a FIRST-CLASS
            // state, branched BEFORE the empty check so a sealed store never falls
            // through to the "Nothing recorded yet" welcome screen.
            storeStateView
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
            isBusy: store.phase != .idle,
            errorText: store.lastError
        )
    }

    // MARK: - Populated

    private var dayCards: [DaysModel.DayCard] { DaysModel.dayCards(index.recordings) }
    private var todayCard: DaysModel.DayCard? { dayCards.first { $0.isToday } }
    private var pastCards: [DaysModel.DayCard] { dayCards.filter { !$0.isToday } }

    private var todayStatus: DaysModel.TodayCaptureStatus {
        DaysModel.todayCaptureStatus(
            isRecording: recorder.state.isRecording,
            startedAt: activeStartedAt,
            ambientEnabled: ambient.enabled,
            ambientActive: ambient.active,
            ambientPaused: ambient.paused
        )
    }

    /// Best-effort start instant for "Recording since" — today's most recently
    /// started recording's start time. Nil before the index reflects it.
    private var activeStartedAt: Date? {
        let today = Calendar.current.startOfDay(for: Date())
        let started = index.recordings
            .filter { $0.startedDay == today }
            .compactMap { $0.startedAt }
            .max()
        return started.map { Date(timeIntervalSince1970: $0) }
    }

    private var populated: some View {
        VStack(alignment: .leading, spacing: 0) {
            if index.usingCLIFallback { fallbackBanner }
            if auth.isGatedForLapse { recordingsSafeBanner }
            if showsMigrationBanner { migrationBanner }
            header
                .padding(.bottom, 22)
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    TodayCardView(
                        status: todayStatus,
                        card: todayCard,
                        frameIndex: frameIndex,
                        thumbnailLoader: thumbnailLoader,
                        dayTasks: dayTasks,
                        appChips: appChips,
                        review: todayCard.map { DaysModel.uploadReview($0.recordings) },
                        onOpen: { openToday() },
                        onReview: { openReview($0) }
                    )
                    .task(id: todayCard?.recordings.map(\.stableID) ?? []) {
                        await resolve(todayCard?.recordings ?? [])
                    }

                    if pastCards.isEmpty {
                        noPastFootageNote
                    } else {
                        ForEach(pastCards) { card in
                            DayCardView(
                                card: card,
                                frameIndex: frameIndex,
                                thumbnailLoader: thumbnailLoader,
                                dayTasks: dayTasks,
                                appChips: appChips,
                                review: DaysModel.uploadReview(card.recordings),
                                onOpen: { onOpenTimeline(card.day, nil) },
                                onReview: { openReview($0) }
                            )
                            .task(id: card.recordings.map(\.stableID)) {
                                await resolve(card.recordings)
                            }
                        }
                    }
                }
                .padding(.bottom, 8)
            }
            .scrollContentBackground(.hidden)
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 28)
    }

    /// Resolve tasks + app chips (at most once each) for a day's recordings so the
    /// card can summarize the day without naming a recording.
    private func resolve(_ recordings: [RecordingSummary]) async {
        for rec in recordings {
            await appChips.resolve(rec)
            await dayTasks.resolve(rec)
        }
    }

    /// Open the day page for today (empty page when there's no footage yet, AE7).
    private func openToday() {
        onOpenTimeline(Calendar.current.startOfDay(for: Date()), nil)
    }

    private func openReview(_ recording: String) {
        openWindow(id: ReviewWindowID, value: recording)
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .center) {
            Text("Days")
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
        Button(action: onOpenSearch) {
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

    /// New-recording CTA — shown only when idle. During capture the recording
    /// banner (pinned above the detail area) is the single control surface.
    @ViewBuilder
    private var newRecordingButton: some View {
        if !recorder.state.isRecording {
            if auth.isGatedForLapse {
                GatedNewRecordingPill(action: onUpgradePrompt)
            } else {
                NewRecordingPill(action: onNewRecording)
            }
        }
    }

    // MARK: - Banners (carried over from the Library surface)

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

    // MARK: - Zero / degraded states

    /// The honest "nothing else recorded yet" note shown below the always-present
    /// Today card when no past day has footage (R15/R21 — never a blank).
    private var noPastFootageNote: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Nothing else recorded yet")
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInkSecondary)
            Text("Days you record will appear here, newest first.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 6)
    }

    @ViewBuilder
    private var errorState: some View {
        if index.lastErrorKind == .staleDaemon {
            staleDaemonError
        } else {
            genericError
        }
    }

    @State private var restarting = false

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

    private func restartHelper() {
        restarting = true
        Task {
            await DaemonInstallController.restartStaleDaemonIfNeeded()
            await index.refresh()
            restarting = false
        }
    }
}

// MARK: - Today card

/// The always-present Today card (R15/AE6): a live capture-status header over the
/// day's task summary + upload/review badge, opening today's day page. Present
/// even before any footage exists.
private struct TodayCardView: View {
    let status: DaysModel.TodayCaptureStatus
    let card: DaysModel.DayCard?
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    @ObservedObject var dayTasks: DayTasks
    @ObservedObject var appChips: DayAppChips
    let review: DaysModel.DayUploadReview?
    var onOpen: () -> Void
    var onReview: (String) -> Void

    @State private var hovering = false

    private var recordings: [RecordingSummary] { card?.recordings ?? [] }

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 12) {
                statusRow
                if !recordings.isEmpty {
                    Divider().overlay(Color.scBorderWarm)
                    DayCardBody(
                        recordings: recordings,
                        frameIndex: frameIndex,
                        thumbnailLoader: thumbnailLoader,
                        dayTasks: dayTasks,
                        appChips: appChips
                    )
                }
                if let review, review.isActionable, let target = review.reviewTarget,
                   let text = review.badgeText {
                    DayReviewBadge(text: text) { onReview(target) }
                }
            }
            .padding(16)
            .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Today, \(DaysModel.todayStatusLine(status))")
    }

    private var statusRow: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Today")
                    .font(SCTypography.serifDayHeading)
                    .foregroundStyle(Color.scInk)
                HStack(spacing: 8) {
                    statusDot
                    Text(DaysModel.todayStatusLine(status))
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(statusColor)
                }
            }
            Spacer(minLength: 0)
            Text("Open day →")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scTeal)
        }
    }

    /// A live pulse dot for the recording state; distinct colors keep paused and
    /// off visually separate (AE6).
    private var statusDot: some View {
        Circle()
            .fill(statusColor)
            .frame(width: 8, height: 8)
            .overlay(
                Circle().stroke(statusColor.opacity(0.35), lineWidth: 3)
            )
    }

    private var statusColor: Color {
        switch status {
        case .recording: return .scTeal
        case .paused: return .scAmberText
        case .starting: return .scInkMuted
        case .off: return .scInkFaint
        }
    }
}

// MARK: - Past day card

/// A single past-day card: the day's task summary + app chip + upload/review
/// badge, opening the day page. No recording title (R5).
private struct DayCardView: View {
    let card: DaysModel.DayCard
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    @ObservedObject var dayTasks: DayTasks
    @ObservedObject var appChips: DayAppChips
    let review: DaysModel.DayUploadReview
    var onOpen: () -> Void
    var onReview: (String) -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(card.label)
                        .font(SCTypography.serifDayHeading)
                        .foregroundStyle(Color.scInk)
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: 0)
                    Text("Open day →")
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scTeal)
                }
                DayCardBody(
                    recordings: card.recordings,
                    frameIndex: frameIndex,
                    thumbnailLoader: thumbnailLoader,
                    dayTasks: dayTasks,
                    appChips: appChips
                )
                if review.isActionable, let target = review.reviewTarget, let text = review.badgeText {
                    DayReviewBadge(text: text) { onReview(target) }
                }
            }
            .padding(16)
            .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        var parts = [card.label]
        let names = card.recordings.flatMap { dayTasks.tasks(for: $0).map(\.name) }
        if let summary = DaysModel.taskSummaryLine(names) { parts.append(summary) }
        return parts.joined(separator: ", ")
    }
}

/// The shared card body: a representative thumbnail plus the day's aggregated
/// task summary and app chip. Reads tasks/apps from the shared resolvers — never
/// renders a recording title (R5).
private struct DayCardBody: View {
    let recordings: [RecordingSummary]
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    @ObservedObject var dayTasks: DayTasks
    @ObservedObject var appChips: DayAppChips

    /// The day's task names, aggregated across its recordings (order preserved).
    private var taskNames: [String] {
        recordings.flatMap { dayTasks.tasks(for: $0).map(\.name) }
    }

    /// The dominant app across the day's recordings (first resolved wins).
    private var app: String? {
        recordings.compactMap { appChips.app(for: $0) }.first
    }

    /// Whether every recording on the day has been task-resolved at least once —
    /// gates the "no tasks named yet" honest note so it doesn't flash early.
    private var resolved: Bool {
        recordings.allSatisfy { dayTasks.hasResolved($0) }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            if let first = recordings.first {
                RecordingCardThumbnail(
                    recording: first,
                    frameIndex: frameIndex,
                    thumbnailLoader: thumbnailLoader,
                    aspectRatio: 16.0 / 9.0,
                    borderColor: .scFillSubtle
                )
                .frame(width: 132)
            }
            VStack(alignment: .leading, spacing: 6) {
                if let summary = DaysModel.taskSummaryLine(taskNames) {
                    Text(summary)
                        .font(SCTypography.sans(size: 13))
                        .foregroundStyle(Color.scInkSecondary)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                } else if resolved {
                    Text("No tasks named yet — still searchable")
                        .font(SCTypography.mono(size: 10.5))
                        .foregroundStyle(Color.scInkMuted)
                }
                if let app {
                    Text(app)
                        .font(SCTypography.mono(size: 10))
                        .foregroundStyle(Color.scInkMuted)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 2)
                        .overlay(
                            RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                        )
                }
            }
            Spacer(minLength: 0)
        }
    }
}

/// The day-level upload/review badge (R14). Tapping opens the Review & upload
/// window on the day's newest cloud-pending recording. Shown only for
/// cloud-destined footage — a local-only day never carries it.
private struct DayReviewBadge: View {
    let text: String
    var onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 6) {
                Image(systemName: "icloud.and.arrow.up")
                    .font(.system(size: 11))
                Text(text)
                    .font(SCTypography.mono(size: 10.5))
            }
            .foregroundStyle(Color.scAmberText)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .overlay(
                Capsule().strokeBorder(Color.scAmberText.opacity(0.4), lineWidth: 1)
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("Review and upload this day's recordings")
    }
}

// MARK: - Header pills (re-homed New-recording affordance)

/// The filled teal New-recording pill on the Days header.
private struct NewRecordingPill: View {
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Circle().fill(Color.scCanvas).frame(width: 8, height: 8)
                Text("New recording")
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
            }
            .foregroundStyle(Color.scCanvas)
            .padding(.horizontal, 20)
            .padding(.vertical, 10)
            .background(hovering ? Color.scTeal.opacity(0.88) : Color.scTeal, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

/// The gated New-recording pill for a lapsed / not-entitled user — a muted
/// outline pill with a lock glyph whose press opens the upgrade prompt (never a
/// silent no-op).
private struct GatedNewRecordingPill: View {
    var action: () -> Void
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
