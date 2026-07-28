import AVKit
import SwiftUI

// U9 — the Day timeline (design 424–470): playback pane on top, the horizontal
// day strip below with recording segments, neutral gaps, provably-blocked
// hatched bands, search-match markers, and a playhead. Seek works across
// chunked video and multiple recordings via DayPlaybackEngine (KTD-12); the
// strip's spans + honesty-split blocked intervals come from `/v0/timeline.day`
// (U3). The search field reuses the existing SearchViewModel scoped to the day.
struct DayTimelineView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @Environment(\.openWindow) private var openWindow

    /// The day the view was OPENED on (the route's day; the `.id` anchor in
    /// MainWindow). In-page date navigation (R16) tracks the currently-viewed
    /// day in `displayedDate`; read `currentDate` everywhere, never `date`.
    let date: Date
    /// Optional wall-clock anchor to land on (a Days card / Recall hit).
    var initialSeekMs: Int?
    /// U4 (AE3) — a task span to emphasize on the strip, populated by a
    /// Tasks/Chat landing. Additive/defaulted so a plain day-card open is
    /// unaffected. Only honored on the opened day (cleared once the user
    /// navigates to another date).
    var highlightedSpan: (startMs: Int, endMs: Int)? = nil
    /// U7 (req 6 / R9) — a range supplied by "delete this day" / "delete this
    /// task" that opens the action menu directly, reusing the range flow. When
    /// present, the day page enters select-range mode with this span preselected
    /// and the menu anchored. Additive/defaulted so a plain day open is
    /// unaffected; U9 wires the day/task delete entry points to it.
    var preselectedRange: (startMs: Int, endMs: Int)? = nil
    var onBack: () -> Void

    private enum LoadPhase: Equatable {
        case loading
        case ready
        case daemonUnavailable
    }

    @StateObject private var engine = DayPlaybackEngine()
    @StateObject private var searchModel = SearchViewModel()
    @State private var loadPhase: LoadPhase = .loading
    @State private var reloading = false
    @State private var spans: [DaySegmentRecording] = []
    /// U5 — store gate: false while the vault is sealed (`store_mounted` on the
    /// `timeline.day` response). Feeds the strip so every empty stretch reads
    /// "can't verify" instead of "nothing on file" when the store is locked.
    @State private var storeMounted = true
    /// Coverage gate: false when a recording with an unreadable `recording.db`
    /// couldn't be placed on the day (`coverage_complete` on the `timeline.day`
    /// response). Feeds the strip so "nothing on file" degrades to "can't
    /// verify" — never a confident data claim over unplaced footage.
    @State private var coverageComplete = true
    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var searchTask: Task<Void, Never>?
    /// U8 (R8/R9) — the composed day-narrative section state. `.hidden` on a
    /// mechanical/thin/absent day (no section), `.stillComposing` while a live day
    /// is being written, `.narrative` when prose exists. Composed from the
    /// per-recording `day.narrative` responses.
    @State private var narrativeState: DayNarrativeState = .hidden

    // SCR-219 (U5) — clip-bounds mode. `clipMode` swaps the playback pane's
    // action buttons for the `ClipBoundsView` overlay; the rest are the working
    // selection state. `clipCenterMs` pins the playhead the clip was started
    // from so a span-picker change re-centers the fixed window on the SAME
    // instant rather than the (possibly moved) live playhead.
    @State private var clipMode = false
    @State private var clipRange: ClipRange?
    @State private var clipSpan: ClipSpan = .thirtySeconds
    @State private var clipSnappedToTask = false
    @State private var clipCenterMs = 0
    @State private var longClipNudgeDismissed = false

    // SCR-214 U11 / U7 — the shared "Select range" mode (KTD-7, R9/R19). ONE
    // two-endpoint selection fronts Clip · Share · Delete, with the SCR-214
    // "save as task" flow layered on top (a range on footage confirms into
    // `tasks.create` via the label sheet). `dayTasks` is the shared write-through
    // layer (also the source of the failed-write alert).
    @StateObject private var dayTasks = DayTasks()
    @State private var rangeSelection = RangeSelectionModel()
    @State private var pendingTaskLabel: PendingTaskLabel?

    // U9 (R8/R19/R20, AE2) — the range-delete flow. `onDeleteRange` loads the
    // `delete.start --dry_run` preview into `.confirming`; confirm tears down the
    // AVPlayer (KTD-12) then drives the execute job through `.deleting` to
    // `.idle` (strip reloads) or `.failed` (surfaced, never silent). A separate
    // `deletePollTask` polls `delete.status` for progress.
    @State private var deletePhase: DeleteRangePhase = .idle
    @State private var deletePollTask: Task<Void, Never>?

    // U11 (R9/R11, F2) — the range Clip/Share flow. `clipRangeConfirm` drives the
    // consent sheet (the single-sourced honesty note + range); confirm calls
    // `clip.create`, keeping the clip silently in Clips (Clip) or handing the cut
    // file to the system share sheet (Share). `clipShareURL` drives the
    // `ShareServicePresenter`. A cross-recording range (KTD-8), a policy-purged
    // range, or a sealed store surfaces via `clipRangeError` — never a silent
    // no-op. The clip catalog is the durable artifact; the Clips surface reads it.
    @State private var clipRangeConfirm: ClipRangeConfirmState?
    @State private var clipRangeWorking = false
    @State private var clipRangeError: String?
    @State private var clipShareURL: URL?

    // U4 — in-page date navigation (R16). `displayedDate` overrides the opened
    // `date` once the user steps days / jumps to a date; `currentDate` is the
    // single source everything reads. `navigatedAway` gates the one-time landing
    // affordances (the initial seek anchor + the task highlight) to the opened
    // day, so navigating elsewhere never re-seeks or re-highlights.
    @State private var displayedDate: Date?
    @State private var navigatedAway = false
    /// U4 (R15/KTD-12) — coalesces live-refresh reloads: a burst of recording
    /// events during one in-flight reload collapses to a single reload.
    @State private var liveReloadInFlight = false

    /// The day currently on screen — the navigated date if the user moved, else
    /// the opened `date`.
    private var currentDate: Date { displayedDate ?? date }

    var body: some View {
        VStack(spacing: 0) {
            header
            // U8 (F1/R8/R9) — the day opens with its written narrative, BELOW the
            // header chrome. Evidence-bound: absent on a mechanical/thin day, a
            // distinct "still composing" note while the live day is being written.
            narrativeSection
            playbackPane
            strip
        }
        .background(Color.scPaper)
        .task(id: currentDate) { await reloadDay() }
        // U4 (R15/KTD-12): one long-lived subscription for the view's lifetime
        // (independent of the date-keyed reload above) — recording events reload
        // the open page so "still recording" + newly-flushed footage stay live.
        // SwiftUI cancels it on disappear.
        .task { await runLiveRefreshSubscription() }
        // U7 (req 6) — a programmatic "delete this day/task" preselection opens
        // the action menu directly. Applied on appear and whenever the supplied
        // range changes so U9's entry points can drive it at any time.
        .onAppear { applyPreselectedRange() }
        .onChange(of: preselectKey) { _ in applyPreselectedRange() }
        .onDisappear {
            searchTask?.cancel()
            deletePollTask?.cancel()
            engine.tearDown()
        }
        .sheet(item: $pendingTaskLabel) { pending in
            MarkTaskLabelSheet(
                startMs: pending.startMs,
                endMs: pending.endMs,
                onSave: { confirmMarkedTask(name: $0) },
                onCancel: { cancelMarkedTask() }
            )
        }
        .alert(
            "Couldn't create the task",
            isPresented: markTaskErrorPresented,
            presenting: dayTasks.writeError
        ) { _ in
            Button("Retry") { Task { await dayTasks.retryLastWrite() } }
            Button("Dismiss", role: .cancel) { dayTasks.dismissWriteError() }
        } message: { err in
            Text(err.message)
        }
        // U9 (R20/AE2) — the delete confirm sheet, which morphs into the delete
        // progress view (one sheet across the Confirm → Deleting transition).
        .sheet(isPresented: deleteSheetPresented) {
            if let content = deleteConfirmContent {
                DeleteConfirmSheet(
                    content: content,
                    // A double-optional: nil while confirming (buttons), .some
                    // while deleting (progress) — see `DeleteConfirmSheet`.
                    deletingFraction: deletePhase.confirmContent != nil
                        ? nil
                        : Optional(deletePhase.deletingFraction),
                    onConfirm: { confirmDeleteRange(content) },
                    onCancel: { cancelDeleteRange() }
                )
            }
        }
        // U9 (R19) — a failed / cancelled / reconfirm-required job surfaces via
        // the same writeError-style alert pattern, never a silent no-op.
        .alert(
            "Couldn't remove that footage",
            isPresented: deleteErrorPresented,
            presenting: deletePhase.failureMessage
        ) { _ in
            Button("OK", role: .cancel) { deletePhase = .idle }
        } message: { message in
            Text(message)
        }
        // U11 (R9/R11, F2) — the range Clip/Share consent sheet (honesty note +
        // range). Confirm cuts the clip; Clip keeps it silently in Clips, Share
        // hands the cut file to the system share sheet.
        .sheet(item: $clipRangeConfirm) { confirm in
            ClipRangeConfirmSheet(
                rangeClockText: confirm.rangeClockText,
                intent: confirm.intent,
                working: clipRangeWorking,
                onConfirm: { confirmClipRange(confirm) },
                onCancel: { cancelClipRange() }
            )
        }
        // U11 — a clip/share failure (cross-recording, policy-purged, sealed
        // store, daemon down) surfaces honestly, never a silent no-op.
        .alert(
            "Couldn't make that clip",
            isPresented: clipRangeErrorPresented,
            presenting: clipRangeError
        ) { _ in
            Button("OK", role: .cancel) { clipRangeError = nil }
        } message: { message in
            Text(message)
        }
        // U11 (F2) — the system share sheet for a just-cut clip file (Share arm).
        // A hidden anchor in the strip's top-leading corner; upload-share stays
        // routed through the Review window (consent boundary unchanged, KTD-4).
        .background(alignment: .topLeading) {
            ShareServicePresenter(item: $clipShareURL)
                .frame(width: 1, height: 1)
                .accessibilityHidden(true)
        }
    }

    /// Bridges `clipRangeError` to an `isPresented` binding for the range
    /// Clip/Share error alert (mirrors the delete-error surfacing).
    private var clipRangeErrorPresented: Binding<Bool> {
        Binding(
            get: { clipRangeError != nil },
            set: { if !$0 { clipRangeError = nil } }
        )
    }

    /// Bridges `dayTasks.writeError` to an `isPresented` binding for the
    /// retroactive create path (mirrors the day surfaces. write-error surfacing).
    private var markTaskErrorPresented: Binding<Bool> {
        Binding(
            get: { dayTasks.writeError != nil },
            set: { if !$0 { dayTasks.dismissWriteError() } }
        )
    }

    // MARK: - U9 delete-flow bindings

    /// The confirm/progress sheet is up while confirming OR deleting. A dismiss
    /// from the sheet chrome (only reachable in the confirming state — the
    /// progress state has no cancel) collapses back to idle without deleting.
    private var deleteSheetPresented: Binding<Bool> {
        Binding(
            get: { deletePhase.sheetPresented },
            set: { presented in
                if !presented, deletePhase.confirmContent != nil { deletePhase = .idle }
            }
        )
    }

    /// The confirm content, shown while confirming and held through the deleting
    /// transition so the sheet keeps its disclosure while the progress runs.
    @State private var deleteContentInFlight: DeleteConfirmContent?

    private var deleteConfirmContent: DeleteConfirmContent? {
        deletePhase.confirmContent ?? deleteContentInFlight
    }

    private var deleteErrorPresented: Binding<Bool> {
        Binding(
            get: { deletePhase.failureMessage != nil },
            set: { if !$0 { deletePhase = .idle } }
        )
    }

    // MARK: - Day window

    private var dayStartMs: Int { Int(Calendar.current.startOfDay(for: currentDate).timeIntervalSince1970 * 1000) }
    /// DST-safe day end — the next local midnight, not a hardcoded +24h (a
    /// "spring forward"/"fall back" day is 23h/25h long).
    private var dayEndMs: Int {
        let cal = Calendar.current
        let start = cal.startOfDay(for: currentDate)
        let next = cal.date(byAdding: .day, value: 1, to: start) ?? start.addingTimeInterval(86_400)
        return Int(cal.startOfDay(for: next).timeIntervalSince1970 * 1000)
    }

    // MARK: - Date navigation (R16)

    /// Jump the viewed day to `newDate` (start-of-day), a no-op when it is
    /// already the viewed day. Marks the page as navigated so the landing seek +
    /// highlight don't fire on the new day. Any date opens — a footage-less date
    /// renders the honest empty page (AE7), never an error.
    private func navigateToDay(_ newDate: Date) {
        let day = Calendar.current.startOfDay(for: newDate)
        guard !Calendar.current.isDate(day, inSameDayAs: currentDate) else { return }
        navigatedAway = true
        displayedDate = day
    }

    /// Step the viewed day by whole days (month/DST-safe via `DayNavigation`).
    private func stepDay(_ delta: Int) {
        navigateToDay(DayNavigation.adjacentDay(to: currentDate, delta: delta))
    }

    /// Binding for the jump-to-date `DatePicker`.
    private var datePickerBinding: Binding<Date> {
        Binding(get: { currentDate }, set: { navigateToDay($0) })
    }

    private var axisBounds: DayStripLayout.Bounds {
        DayStripLayout.axisBounds(
            dayStartMs: dayStartMs,
            dayEndMs: dayEndMs,
            spans: spans.map { (startMs: $0.startMs, endMs: $0.endMs) }
        )
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 16) {
            Button(action: onBack) {
                Text("← Back")
                    .font(SCTypography.sans(size: 13))
                    .foregroundStyle(Color.scInkSecondary)
            }
            .buttonStyle(.plain)
            .help("Back")
            dateNavigator
            Spacer()
            searchField
            dayActionsMenu
        }
        // The day page's content gutter. The playback pane insets by the same
        // value so the player's edges align with this row (ShellWindowLayout).
        .padding(.horizontal, ShellWindowLayout.dayHeaderHorizontalPadding)
        .padding(.vertical, 16)
        .background(Color.scCanvas)
        .overlay(alignment: .bottom) { Divider().overlay(Color.scBorderWarm) }
    }

    // MARK: - Narrative (U8, F1/R8/R9)

    /// The written day narrative, or its honest states. Rendered only on the
    /// opened day while the store is mounted; a locked/absent store defers to the
    /// strip's own "can't verify" honesty, never a stale narrative.
    @ViewBuilder
    private var narrativeSection: some View {
        if storeMounted {
            switch narrativeState {
            case .narrative(let text):
                narrativeCard(text)
            case .stillComposing:
                stillComposingNote
            case .hidden:
                EmptyView()
            }
        }
    }

    /// The narrative prose card (R8) — the day's written summary, composed from
    /// block evidence (partial on a mixed day, KTD-10).
    private func narrativeCard(_ text: String) -> some View {
        Text(text)
            .font(SCTypography.sans(size: 13.5))
            .foregroundStyle(Color.scInk)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .accessibilityLabel("Day summary. \(text)")
    }

    /// The DISTINCT "still composing" state (R10): real blocks exist but the
    /// narrative isn't written yet (a live day in progress). Never a blank slot
    /// that reads as "nothing to say".
    private var stillComposingNote: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("Still writing today's summary…")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .accessibilityLabel("Still writing today's summary")
    }

    /// U9 (req 5, R9) — the day-page overflow menu hosting "Delete this day…".
    /// It preselects the day's whole footage extent (U7's preselect) and enters
    /// the SAME confirm flow a range delete uses (day/task deletes are the same
    /// action with a preselected range). Disabled on a footage-less day.
    private var dayActionsMenu: some View {
        Menu {
            Button("Delete this day…", role: .destructive) { beginDayDelete() }
                .disabled(dayFootageExtent == nil)
        } label: {
            Image(systemName: "ellipsis.circle")
                .font(.system(size: 15))
                .foregroundStyle(Color.scInkSecondary)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Day actions")
        .accessibilityLabel("Day actions")
    }

    /// The day's whole footage extent (absolute unix ms) — the union of every
    /// recording's span. Nil on a footage-less day (nothing to delete).
    private var dayFootageExtent: (startMs: Int, endMs: Int)? {
        guard let lo = spans.map(\.startMs).min(),
              let hi = spans.map(\.endMs).max(),
              hi > lo else { return nil }
        return (lo, hi)
    }

    /// U9 (req 5) — "Delete this day": preselect the full-day footage extent (so
    /// the strip shows the selection band) and open the same delete confirm flow.
    /// The Days/Tasks surfaces call this shape via `preselectedRange`; the daemon
    /// still chunk-rounds + trims to footage, so an over-wide extent is safe.
    private func beginDayDelete() {
        guard let extent = dayFootageExtent else { return }
        rangeSelection.preselect(startMs: extent.startMs, endMs: extent.endMs)
        onDeleteRange((startMs: extent.startMs, endMs: extent.endMs))
    }

    /// R16 — previous/next-day chevrons + a jump-to-date picker around the viewed
    /// date. Any date opens (honest empty page on a footage-less date, AE7).
    private var dateNavigator: some View {
        HStack(spacing: 10) {
            Button { stepDay(-1) } label: {
                Image(systemName: "chevron.left").font(.system(size: 12, weight: .semibold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.scInkSecondary)
            .help("Previous day")
            .accessibilityLabel("Previous day")

            Text(Self.headerDateFormatter.string(from: currentDate))
                .font(SCTypography.grotesk(size: 15, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .frame(minWidth: ShellWindowLayout.dayDateLabelMinWidth, alignment: .leading)

            Button { stepDay(1) } label: {
                Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.scInkSecondary)
            .help("Next day")
            .accessibilityLabel("Next day")

            // A compact graphical calendar affordance for jumping to any date.
            DatePicker("", selection: datePickerBinding, displayedComponents: [.date])
                .datePickerStyle(.field)
                .labelsHidden()
                .accessibilityLabel("Jump to a date")
        }
    }

    private var searchField: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12))
                .foregroundStyle(Color.scTeal)
            TextField("Search this day", text: $query)
                .textFieldStyle(.plain)
                .font(SCTypography.sans(size: 13))
                .onSubmit { runDayScopedSearch() }
            if !dayMatchesMs.isEmpty {
                Text("\(dayMatchesMs.count) moment\(dayMatchesMs.count == 1 ? "" : "s")")
                    .font(SCTypography.mono(size: 10))
                    .foregroundStyle(Color.scTeal)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        // Was a fixed 300pt, which made the field the largest single hard
        // minimum in the header and left it nothing to give back when the
        // window got tight. A range keeps the preferred width while letting the
        // header compress instead of overflowing (see ShellWindowLayout).
        .frame(
            minWidth: ShellWindowLayout.daySearchFieldMinWidth,
            maxWidth: ShellWindowLayout.daySearchFieldMaxWidth
        )
        .background(Color.scSurface, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.scTeal.opacity(0.4), lineWidth: 1))
    }

    // MARK: - Playback pane

    @ViewBuilder
    private var playbackPane: some View {
        ZStack {
            switch engine.target {
            case .media:
                AVPlayerNSView(player: engine.player)
            case .placeholder(let reason):
                CardHatchPlaceholder()
                VStack(spacing: 12) {
                    Text(placeholderCaption(reason))
                        .font(SCTypography.mono(size: 12))
                        .foregroundStyle(Color.scInkMuted)
                        .multilineTextAlignment(.center)
                    // The daemon-down caption tells the user to retry, so give
                    // them the control to do it — a stale-daemon restart (a
                    // no-op if the daemon isn't stale) followed by a reload,
                    // mirroring the Days surface.s staleDaemonError affordance.
                    // Without this the message is an instruction with no button.
                    if loadPhase == .daemonUnavailable {
                        Button(reloading ? "Retrying…" : "Retry") { retryDay() }
                            .disabled(reloading)
                    }
                }
                .padding(.horizontal, 24)
            }
        }
        // SCR-297 — the pane takes the shape of the footage rather than the
        // shape of the leftover space. Before this it filled its box and let
        // AVPlayerView pillarbox inside, which misbehaved across the whole size
        // range: at a short window the leftover collapsed to a ~3:1 box against
        // 16:10 footage (fixed by the floor below), and at the default and
        // wider the video drew ~710pt inside a ~932pt pane, leaving black bars
        // either side that no amount of sizing removed.
        //
        // A nil aspect deliberately reproduces that older fill-the-box layout
        // exactly: it is the pre-.readyToPlay state and the placeholder state,
        // and filling is the known-safe rendering. The pane never guesses a
        // ratio — a wrong one would mis-shape the player AND mis-anchor the
        // chrome below, which is worse than the bars it replaces.
        .modifier(SourceAspectFit(aspect: engine.sourceAspect))
        // The overlays sit INSIDE the aspect constraint and OUTSIDE nothing
        // else: this is what finally makes them track the video's own edges
        // rather than the pane's. While the pane was aspect-free the two were
        // not the same rectangle, so at wide window sizes the timestamp chip
        // and action bar floated out over the pillarbox bars.
        .overlay(alignment: .bottomLeading) { timestampChip }
        .overlay(alignment: .bottomTrailing) { if !clipMode { actionButtons } }
        .overlay(alignment: .bottom) { clipBoundsOverlay }
        // The slot, OUTSIDE the overlays: it holds the pane's height floor and
        // centres the constrained player in whatever space the header,
        // narrative and strip leave. The space it does not use falls through to
        // the day page's own background rather than reading as dead player
        // surface — which is the point of the constraint above.
        .frame(
            maxWidth: .infinity,
            minHeight: ShellWindowLayout.playbackPaneMinHeight,
            maxHeight: .infinity
        )
        // The day page's content gutter, so the player's edges line up with the
        // date navigator above it. PlaybackAspect.fittedSize applies the same
        // inset when computing the fitted size, so the two agree by contract.
        .padding(.horizontal, ShellWindowLayout.playbackPaneHorizontalInset)
        // KTD-10: the Inspect window survives only as a day-page footage DEBUG
        // entry (it also hosts Undated recordings) — never a browsing or citation
        // path. Chat / search citations route here to the day page instead (U12).
        .contextMenu { footageDebugMenu }
    }

    /// KTD-10 — the per-recording Inspect window as a right-click DEBUG affordance
    /// over footage. Only offered when a recording sits under the playhead; opens
    /// the Inspect window seeked to the current moment. Labeled "(debug)" so it
    /// never reads as a primary affordance; it is the one surviving reach into the
    /// Inspect window (and the host for Undated recordings), NOT a citation path.
    @ViewBuilder
    private var footageDebugMenu: some View {
        if let name = engine.currentRecording {
            Button("Inspect recording (debug)") {
                if let ms = engine.currentDayMs {
                    InspectWindowOpener.shared.pendingSeekMs[name] = ms
                }
                InspectWindowOpener.shared.open(recordingName: name)
            }
        }
    }

    private func placeholderCaption(_ reason: DayPlaceholderReason) -> String {
        switch loadPhase {
        case .loading: return "loading day…"
        case .daemonUnavailable: return "day view needs the background helper — start Screencap's helper and retry"
        case .ready:
            switch reason {
            case .nothingCaptured:
                // R7 both ways: inside a recording span, activity *was*
                // captured — only the video frame is missing (e.g. the moment
                // precedes the first written frame), so "nothing captured"
                // would over-claim in the other direction.
                return playheadInsideRecordingSpan
                    ? "no video frames for this moment"
                    : "nothing captured at this time"
            // R7: the moment existed but its local media was evicted after
            // upload — "nothing captured" would be a false claim.
            case .mediaUnavailable: return "local media removed after upload"
            }
        }
    }

    private var playheadInsideRecordingSpan: Bool {
        guard let ms = engine.currentDayMs else { return false }
        return spans.contains { $0.startMs <= ms && ms < $0.endMs }
    }

    private var timestampChip: some View {
        Group {
            if let ms = engine.currentDayMs {
                Text(Self.clockFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000)))
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scCanvas)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.scInk.opacity(0.85), in: RoundedRectangle(cornerRadius: 5))
                    .padding(14)
                    .accessibilityLabel(DayStripAccessibility.playheadLabel(ms: ms))
            }
        }
    }

    /// The playback pane's bottom-trailing action area. In select-range mode
    /// (U7) it becomes the endpoint-marking / fine-tune bar; otherwise it shows
    /// the default per-moment affordances (Select range · Clip this moment ·
    /// Share from here).
    @ViewBuilder
    private var actionButtons: some View {
        if rangeSelection.isActive {
            rangeModeButtons
        } else {
            defaultActionButtons
        }
    }

    private var defaultActionButtons: some View {
        HStack(spacing: 8) {
            selectRangeButton
            clipButton
            if let recording = shareableRecording {
                Button("Share from here") {
                    // Upload consent stays load-bearing: sharing routes through
                    // the existing Review window flow (KTD-4).
                    openWindow(id: ReviewWindowID, value: recording)
                }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scCanvas)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.scTeal, in: Capsule())
                .help("Review and upload this recording")
            }
        }
        .padding(14)
    }

    /// U7 (R9/R19) — enter the explicit "Select range" mode: the shared entry
    /// for Clip · Share · Delete (and, on footage, save-as-task). The strip then
    /// takes two playhead endpoints (KTD-7 — no drag).
    private var selectRangeButton: some View {
        Button("Select range") { rangeSelection.enterSelectMode() }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: .semibold))
            .foregroundStyle(Color.scCanvas)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(Color.scInk.opacity(0.85), in: Capsule())
            .help("Select a time range to clip, share, or delete")
    }

    /// U7 — the in-mode bottom bar. While placing endpoints it marks start/end
    /// from the playhead; once a range is chosen it swaps to arrow-key-nudge
    /// fine-tune controls. Cancel (and Esc, R19) always exits the mode.
    @ViewBuilder
    private var rangeModeButtons: some View {
        HStack(spacing: 8) {
            if rangeSelection.range == nil {
                Button(rangeMarkTitle) { markRangeEndpoint() }
                    .buttonStyle(.plain)
                    .font(SCTypography.sans(size: 12, weight: .semibold))
                    .foregroundStyle(engine.currentDayMs != nil ? Color.scCanvas : Color.scCanvas.opacity(0.6))
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                    .background(Color.scInk.opacity(0.85), in: Capsule())
                    .disabled(engine.currentDayMs == nil)
                    .help("Seek the playhead, then set this endpoint")
            } else {
                rangeNudgeControls
            }
            Button("Cancel") { rangeSelection.cancel() }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scCanvas)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.scRust.opacity(0.85), in: Capsule())
                .keyboardShortcut(.cancelAction)  // Esc exits the mode (R19)
                .help("Cancel range selection (Esc)")
        }
        .padding(14)
    }

    private var rangeMarkTitle: String {
        rangeSelection.pendingEndpointMs == nil ? "Set range start" : "Set range end"
    }

    /// U7 (req 2) — arrow-key-nudge fine-tune for the chosen range: 1 px ≈
    /// minutes on a 13 h axis, so precise endpoints need a keyboard step
    /// (`RangeSelectionModel.nudgeStepMs`). Left/Right nudge the end; Shift+Left/
    /// Right nudge the start; the visible chevrons keep it discoverable.
    private var rangeNudgeControls: some View {
        let step = RangeSelectionModel.nudgeStepMs
        return HStack(spacing: 6) {
            Text("start")
                .font(SCTypography.mono(size: 10))
                .foregroundStyle(Color.scCanvas.opacity(0.8))
            nudgeButton(system: "chevron.left") { rangeSelection.nudgeStart(byMs: -step) }
                .keyboardShortcut(.leftArrow, modifiers: .shift)
            nudgeButton(system: "chevron.right") { rangeSelection.nudgeStart(byMs: step) }
                .keyboardShortcut(.rightArrow, modifiers: .shift)
            Text("end")
                .font(SCTypography.mono(size: 10))
                .foregroundStyle(Color.scCanvas.opacity(0.8))
            nudgeButton(system: "chevron.left") { rangeSelection.nudgeEnd(byMs: -step) }
                .keyboardShortcut(.leftArrow, modifiers: [])
            nudgeButton(system: "chevron.right") { rangeSelection.nudgeEnd(byMs: step) }
                .keyboardShortcut(.rightArrow, modifiers: [])
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color.scInk.opacity(0.85), in: Capsule())
        .help("Fine-tune the endpoints: arrow keys nudge the end, Shift+arrows the start")
    }

    private func nudgeButton(system: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: system).font(.system(size: 11, weight: .semibold))
        }
        .buttonStyle(.plain)
        .foregroundStyle(Color.scCanvas)
    }

    /// SCR-219 (U5) — "Clip this moment", enabled for a clippable recording
    /// (`isClippable`: local, non-stub, video present — R8), disabled otherwise
    /// so the design's affordance stays present. Tapping enters clip-bounds
    /// mode. Replaces the shipped disabled stub (R9).
    @ViewBuilder
    private var clipButton: some View {
        let clippable = clippableRecordingName != nil
        Button("Clip this moment") { if clippable { beginClip() } }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: clippable ? .semibold : .regular))
            .foregroundStyle(clippable ? Color.scCanvas : Color.scCanvas.opacity(0.6))
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(Color.scInk.opacity(0.85), in: Capsule())
            .disabled(!clippable)
            .help(clippable
                ? "Clip this moment to a local video file"
                : "Clipping isn't available for this recording")
    }

    /// Snap boundaries for the current day (each recording's edges + each existing
    /// task band's edges) so a range endpoint abuts its neighbours cleanly.
    private var markSnapBoundaries: [Int] {
        DaySpanSnap.boundaries(baseTracks: stripBaseTracks, segments: stripSegments)
    }

    /// The pending selection band + lone-endpoint tick handed to the strip —
    /// now sourced from the shared select-range mode (U7). VoiceOver-perceivable
    /// via the strip's a11y overlays (R19).
    private var stripPendingSelection: (startMs: Int, endMs: Int)? {
        rangeSelection.range
    }

    private var stripPendingEndpoint: Int? {
        rangeSelection.pendingEndpointMs
    }

    // MARK: - Range selection (U7)

    /// Capture the playhead as the next range endpoint, snapped to the nearest
    /// task/recording boundary. R19: the selection MAY span a "nothing captured"
    /// gap — there is NO silent reset when the span or its midpoint lands in a
    /// gap (the old mark-a-task behavior is gone); the anchored menu simply
    /// offers what applies. On completion the action menu is presented.
    private func markRangeEndpoint() {
        guard let playheadMs = engine.currentDayMs else { return }
        let snapped = DaySpanSnap.snap(playheadMs, to: markSnapBoundaries)
        rangeSelection.mark(snapped)
        if rangeSelection.range != nil { rangeSelection.presentMenu() }
    }

    /// The recording under the selection's midpoint, if any — gates "save as
    /// task" (and, in U11, single-recording clip/share). Nil when the midpoint
    /// is in a gap: the selection is KEPT (R19); the task action is just absent.
    private func rangeMidpointRecording(_ range: (startMs: Int, endMs: Int)) -> String? {
        DaySpanSnap.recording(forRangeMidpoint: range.startMs, range.endMs, baseTracks: stripBaseTracks)
    }

    /// SCR-214 kept working ON TOP of the range mode: confirm the chosen span as
    /// a task (label sheet → `tasks.create`) when a recording sits under the
    /// midpoint. A midpoint-in-gap span is NOT discarded — this action is simply
    /// not offered for it.
    private func saveRangeAsTask(_ range: (startMs: Int, endMs: Int)) {
        guard let recording = rangeMidpointRecording(range) else { return }
        pendingTaskLabel = PendingTaskLabel(recording: recording, startMs: range.startMs, endMs: range.endMs)
    }

    // U7 exposed these DayTimelineView-owned hooks for the range action menu; U11
    // fills Clip/Share (via `/v0/clip.create` + the share sheet). Both resolve the
    // range to a SINGLE recording first (KTD-8) — a range crossing recordings, or
    // one over a pure gap, gets an honest error naming the split rather than a
    // silent no-op. (Named `on…Range` to stay distinct from the SCR-219
    // `clipRange` state.)
    private func onClipRange(_ range: (startMs: Int, endMs: Int)) {
        beginClipRangeAction(range, intent: .clip)
    }

    private func onShareRange(_ range: (startMs: Int, endMs: Int)) {
        beginClipRangeAction(range, intent: .share)
    }

    /// U11 — resolve the selected range to one recording (KTD-8) and open the
    /// consent sheet, or surface the honest cross-recording / no-footage error.
    private func beginClipRangeAction(_ range: (startMs: Int, endMs: Int), intent: ClipRangeIntent) {
        let tracks = stripBaseTracks.map {
            ClipsModel.RecordingTrack(recording: $0.recording, startMs: $0.startMs, endMs: $0.endMs)
        }
        switch ClipsModel.resolveRange(startMs: range.startMs, endMs: range.endMs, tracks: tracks) {
        case .single(let recording):
            clipRangeWorking = false
            clipRangeConfirm = ClipRangeConfirmState(
                recording: recording, startMs: range.startMs, endMs: range.endMs, intent: intent
            )
        case .crossesRecordings(let splitMs):
            clipRangeError = ClipsModel.crossRecordingMessage(splitMs: splitMs)
        case .noFootage:
            clipRangeError = ClipsModel.noFootageMessage
        }
    }

    /// U11 — the user confirmed the clip/share. Cut the clip via `clip.create`
    /// (creator `ui`); on success keep it silently in Clips (Clip) or hand the cut
    /// file to the system share sheet (Share). A clip-domain reason
    /// (policy-purged, not-eligible, …) or a thrown error surfaces honestly.
    private func confirmClipRange(_ confirm: ClipRangeConfirmState) {
        clipRangeWorking = true
        Task {
            let tzOffset = TimeZone.current.secondsFromGMT(
                for: Date(timeIntervalSince1970: Double(confirm.startMs) / 1000)
            )
            do {
                let response = try await DaemonClient.clipCreate(
                    recording: confirm.recording,
                    startMs: confirm.startMs,
                    endMs: confirm.endMs,
                    tzOffsetSeconds: tzOffset,
                    creator: "ui"
                )
                clipRangeWorking = false
                clipRangeConfirm = nil
                guard response.ok, let clip = response.clip else {
                    // Clip-domain failure rides the envelope (ok=false + reason).
                    clipRangeError = response.reason.map(ClipsModel.reasonMessage)
                        ?? "Couldn't make that clip."
                    return
                }
                // The clip is the durable artifact now — leave select-range mode.
                rangeSelection.cancel()
                if confirm.intent == .share, let path = clip.path {
                    clipShareURL = URL(fileURLWithPath: path)
                }
                // Clip intent: saved silently into Clips (no further UI, R11).
            } catch {
                clipRangeWorking = false
                clipRangeConfirm = nil
                clipRangeError = ClipsModel.errorMessage(error)
            }
        }
    }

    /// Cancel the clip/share consent sheet (R19 — explicit, never silent). Keeps
    /// the range selection so the anchored menu stays for another action.
    private func cancelClipRange() {
        clipRangeConfirm = nil
        clipRangeWorking = false
    }

    /// U9 (R20/AE2) — open the delete confirm flow: load the `delete.start
    /// --dry_run` preview (the ACTUAL rounded extent + kept overlapping clips +
    /// live-chunk exclusion), then present the confirm sheet. A daemon error
    /// (sealed store, bad range, daemon down) surfaces via the error alert — never
    /// a silent no-op. The selection is KEPT while confirming so a cancel returns
    /// to the anchored menu.
    private func onDeleteRange(_ range: (startMs: Int, endMs: Int)) {
        Task {
            do {
                let preview = try await DaemonClient.deleteStartPreview(
                    startMs: range.startMs, endMs: range.endMs
                )
                deletePhase = .confirming(DeleteConfirmContent.from(preview: preview))
            } catch {
                deletePhase = .failed(message: DeleteRangeFailure.fromError(error))
            }
        }
    }

    /// U9 — the user confirmed the delete (Confirm → Deleting). FIRST tear down
    /// the AVPlayer (KTD-12 — open file handles keep deleted bytes playable on
    /// APFS), then fire the execute job with the preview's confirm token and poll
    /// `delete.status` for progress. A `reconfirm_required` result tells the user
    /// the footage changed and to re-select; a completed job reloads the day; any
    /// failure surfaces via the alert.
    private func confirmDeleteRange(_ content: DeleteConfirmContent) {
        guard content.isDeletable else { cancelDeleteRange(); return }
        // Tear down playback BEFORE deleting so no open handle pins deleted bytes.
        engine.tearDown()
        deleteContentInFlight = content
        deletePhase = .deleting(fraction: nil)
        deletePollTask?.cancel()
        deletePollTask = Task { await runDeleteJob(content) }
    }

    /// Cancel the confirm sheet (R19 — an explicit dismiss, never silent). Keeps
    /// the range selection so the anchored menu stays for another action.
    private func cancelDeleteRange() {
        deletePhase = .idle
    }

    /// Drive the execute job to a terminal state, polling `delete.status` for the
    /// determinate progress bar. On completion: leave select-range mode + reload
    /// the day so the strip shows "removed by you". On reconfirm/failed/cancelled:
    /// surface the honest message.
    private func runDeleteJob(_ content: DeleteConfirmContent) async {
        do {
            var snapshot = try await DaemonClient.deleteStartExecute(
                startMs: content.requestedStartMs,
                endMs: content.requestedEndMs,
                resolved: content.resolved
            )
            // Poll until the job leaves a running/idle state. A SINGLE status
            // read can blip (socket hiccup, daemon busy) while the background
            // job keeps deleting — do NOT treat one nil as terminal, or we'd
            // alert "nothing was removed" while the reloaded strip shows the
            // footage gone. Tolerate a few consecutive failures; only if the
            // stream stays unreadable do we give up, honestly (unconfirmed).
            var consecutivePollFailures = 0
            while !Task.isCancelled, snapshot.state == "running" || snapshot.state == "idle" {
                deletePhase = .deleting(fraction: snapshot.fraction)
                try? await Task.sleep(nanoseconds: 400_000_000)
                if Task.isCancelled { return }
                guard let next = try? await DaemonClient.deleteStatus() else {
                    consecutivePollFailures += 1
                    if consecutivePollFailures >= 4 {
                        deleteContentInFlight = nil
                        deletePhase = .failed(message: DeleteRangeFailure.unconfirmed)
                        await reloadDay()
                        return
                    }
                    continue
                }
                consecutivePollFailures = 0
                snapshot = next
            }
            if Task.isCancelled { return }
            await finishDeleteJob(snapshot)
        } catch {
            // The execute call itself threw (daemon down, sealed store). Nothing
            // was deleted; restore the torn-down engine and surface the error.
            deleteContentInFlight = nil
            deletePhase = .failed(message: DeleteRangeFailure.fromError(error))
            await reloadDay()
        }
    }

    /// Resolve a terminal delete snapshot to the final UI state. The AVPlayer was
    /// torn down before the job (KTD-12), so EVERY path reloads the day to restore
    /// a live engine + the current strip — on success the strip now carries the
    /// "removed by you" band; on failure nothing was deleted and playback recovers.
    private func finishDeleteJob(_ snapshot: DeleteStatusResponse) async {
        deleteContentInFlight = nil
        if snapshot.reconfirmRequired || snapshot.state == "reconfirm_required" {
            // KTD-3 no-TOCTOU: the resolved set changed; nothing was deleted.
            deletePhase = .failed(message: DeleteRangeFailure.reconfirm)
        } else if snapshot.state == "completed" {
            rangeSelection.cancel()  // leave select-range mode
            deletePhase = .idle
        } else {
            // failed / cancelled / any unknown terminal state — surface honestly,
            // never claim success.
            deletePhase = .failed(message: DeleteRangeFailure.job(state: snapshot.state))
        }
        await reloadDay()  // restore the torn-down engine + re-render the strip
    }

    /// U7 (req 6) — apply an externally supplied preselected range: enter
    /// select-range mode with the span set and the menu anchored (day/task
    /// delete reuses the same flow).
    private func applyPreselectedRange() {
        guard let range = preselectedRange else { return }
        rangeSelection.preselect(startMs: range.startMs, endMs: range.endMs)
    }

    /// A change key for `onChange` (tuples aren't Equatable).
    private var preselectKey: String? {
        preselectedRange.map { "\($0.startMs)-\($0.endMs)" }
    }

    /// Confirm the label → `tasks.create` over the selected span, then reload the
    /// day so the new band renders. A failed write surfaces on `dayTasks`.
    private func confirmMarkedTask(name: String) {
        guard let pending = pendingTaskLabel else { return }
        pendingTaskLabel = nil
        rangeSelection.cancel()  // task committed → leave select-range mode
        Task {
            let ok = await dayTasks.create(
                recording: pending.recording,
                name: name,
                startTs: Double(pending.startMs) / 1000,
                endTs: Double(pending.endMs) / 1000
            )
            if ok { await reloadDay() }
        }
    }

    private func cancelMarkedTask() {
        // Cancelling the label sheet keeps the range selection (R19 — an
        // explicit sheet-cancel must not silently discard the span); the
        // anchored menu stays so the user can pick a different action.
        pendingTaskLabel = nil
    }

    /// The clip-bounds selection panel, shown at the bottom of the playback
    /// pane while `clipMode` is active. Nil-guards fall back to nothing so an
    /// edge case (recording changed out from under clip mode) can't crash.
    @ViewBuilder
    private var clipBoundsOverlay: some View {
        if clipMode,
           let range = clipRange,
           let footage = currentFootageBounds {
            ClipBoundsView(
                range: Binding(get: { clipRange ?? range }, set: { clipRange = $0 }),
                footageStartMs: footage.start,
                footageEndMs: footage.end,
                snappedToTask: clipSnappedToTask,
                span: clipSpan,
                showNudge: ClipBoundsResolver.isLongClip(range) && !longClipNudgeDismissed,
                onSelectSpan: { selectClipSpan($0) },
                onDismissNudge: { longClipNudgeDismissed = true },
                onReview: { confirmClip() },
                onCancel: { exitClipMode() }
            )
        }
    }

    /// The recording under the playhead, when it can still go through the
    /// Review-before-upload flow.
    private var shareableRecording: String? {
        guard let name = engine.currentRecording,
              let summary = index.recordings.first(where: { $0.name == name }),
              summary.isUploadEligible else { return nil }
        return name
    }

    /// The recording under the playhead, when it is clippable (R8). Distinct
    /// from `shareableRecording`: an already-uploaded (but still local)
    /// recording is clippable even though it is not upload-eligible.
    private var clippableRecordingName: String? {
        guard let name = engine.currentRecording,
              let summary = index.recordings.first(where: { $0.name == name }),
              summary.isClippable else { return nil }
        return name
    }

    /// Available footage bounds (absolute unix ms) for the recording under the
    /// playhead — the clamp window for clip bounds. A recording's day spans are
    /// unioned (a recording could surface as more than one strip segment).
    private var currentFootageBounds: (start: Int, end: Int)? {
        guard let name = engine.currentRecording else { return nil }
        let recSpans = spans.filter { $0.name == name }
        guard let start = recSpans.map(\.startMs).min(),
              let end = recSpans.map(\.endMs).max(),
              end > start else { return nil }
        return (start, end)
    }

    // MARK: - Clip bounds mode

    /// Enter clip-bounds mode: resolve the default range (snap to the labeled
    /// moment under the playhead via `tasks.list`, else a centered fixed
    /// window), then show the `ClipBoundsView` overlay (R1/R3).
    private func beginClip() {
        guard let playheadMs = engine.currentDayMs,
              let footage = currentFootageBounds else { return }
        clipCenterMs = playheadMs
        longClipNudgeDismissed = false
        Task {
            await engine.refreshTasksForCurrentRecording()
            if let task = ClipBoundsResolver.containingTask(
                tasks: engine.currentRecordingTasks, playheadMs: playheadMs
            ) {
                clipSnappedToTask = true
                clipRange = ClipBoundsResolver.momentBounds(
                    task: task, footageStartMs: footage.start, footageEndMs: footage.end
                )
            } else {
                clipSnappedToTask = false
                clipRange = ClipBoundsResolver.fixedWindow(
                    centerMs: playheadMs, span: clipSpan,
                    footageStartMs: footage.start, footageEndMs: footage.end
                )
            }
            clipMode = true
        }
    }

    /// Change the fixed-window span (fallback only) — recompute the window
    /// centered on the ORIGINAL clip center, clamped to footage (R3).
    private func selectClipSpan(_ span: ClipSpan) {
        clipSpan = span
        guard !clipSnappedToTask, let footage = currentFootageBounds else { return }
        clipRange = ClipBoundsResolver.fixedWindow(
            centerMs: clipCenterMs, span: span,
            footageStartMs: footage.start, footageEndMs: footage.end
        )
    }

    /// Confirm the bounds: carry the range out-of-band via `pendingClipRange`
    /// and open the Review window keyed on the recording name (KTD5), then exit
    /// bounds mode. The Review window (U6) consumes the range on `.ready`.
    private func confirmClip() {
        guard let name = clippableRecordingName, let range = clipRange else {
            exitClipMode()
            return
        }
        ReviewWindowOpener.shared.pendingClipRange[name] = range
        openWindow(id: ReviewWindowID, value: name)
        exitClipMode()
    }

    /// Leave clip-bounds mode without opening Review (the named Cancel, R9).
    private func exitClipMode() {
        clipMode = false
        clipRange = nil
        clipSnappedToTask = false
        longClipNudgeDismissed = false
    }

    // MARK: - Strip

    private var strip: some View {
        VStack(alignment: .leading, spacing: 0) {
            DayStripView(
                bounds: axisBounds,
                baseTracks: stripBaseTracks,
                segments: stripSegments,
                blockedBands: DayStripBlockedBand.provenBands(from: spans),
                matchesMs: dayMatchesMs,
                playheadMs: engine.currentDayMs,
                onSeek: { engine.seek(toDayMs: $0) },
                // U5 — provenance: purged spans (R10/R6), unverifiable
                // intervals, the gap-cause inputs, and the load/store gates.
                purgedBands: DayPurgedInterval.bands(from: spans),
                // U9 (R8) — user range-deletes ("removed by you"), distinct from
                // the policy-purged band.
                deletedBands: DayDeletedInterval.bands(from: spans),
                unverifiableBands: spans.flatMap { span in
                    span.unverifiable.map { (startMs: $0.startMs, endMs: $0.endMs) }
                },
                spanProvenance: spans.map {
                    DayStripLayout.SpanProvenance(
                        startMs: $0.startMs, endMs: $0.endMs, endStatus: $0.endStatus
                    )
                },
                storeMounted: storeMounted,
                coverageComplete: coverageComplete,
                provenanceReady: loadPhase == .ready,
                pendingSelection: stripPendingSelection,
                pendingEndpointMs: stripPendingEndpoint,
                // U4 (AE3): honor the landing highlight only on the opened day —
                // navigating to another date drops it.
                highlightedSpan: navigatedAway ? nil : highlightedSpan
            )
            // U7 (R9) — the action menu anchors over the completed selection,
            // clamped inside the strip's width via the same axis mapping.
            .overlay(alignment: .topLeading) { rangeActionMenuOverlay }
        }
        .padding(.horizontal, 24)
        .padding(.top, 18)
        .padding(.bottom, 20)
        .background(Color.scPaper)
        .overlay(alignment: .top) { Divider().overlay(Color.scBorderWarm) }
    }

    /// U7 (R9) — the action menu anchored over the chosen selection: Clip ·
    /// Share · Delete (+ Save as task when the span sits on footage). Positioned
    /// near the selection's leading edge, clamped inside the strip. The overlay
    /// takes the strip's frame (so its GeometryReader width matches the axis
    /// mapping); it appears only while a range is chosen.
    @ViewBuilder
    private var rangeActionMenuOverlay: some View {
        if rangeSelection.menuAnchored, let range = rangeSelection.range {
            GeometryReader { geo in
                let menuWidth: CGFloat = 320
                let leadingX = DayStripLayout.x(forMs: range.startMs, bounds: axisBounds, width: geo.size.width)
                let clampedX = min(max(0, leadingX), max(0, geo.size.width - menuWidth))
                rangeActionMenu(range: range)
                    .frame(width: menuWidth, alignment: .leading)
                    .offset(x: clampedX, y: -6)
            }
        }
    }

    private func rangeActionMenu(range: (startMs: Int, endMs: Int)) -> some View {
        HStack(spacing: 10) {
            Text("\(DayStripAccessibility.hourMinuteText(ms: range.startMs))–\(DayStripAccessibility.hourMinuteText(ms: range.endMs))")
                .font(SCTypography.mono(size: 10))
                .foregroundStyle(Color.scInkSecondary)
            menuAction("Clip") { onClipRange(range) }
            menuAction("Share") { onShareRange(range) }
            menuAction("Delete", destructive: true) { onDeleteRange(range) }
            if rangeMidpointRecording(range) != nil {
                menuAction("Save as task") { saveRangeAsTask(range) }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.scCanvas, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.scBorderWarm, lineWidth: 1))
        .shadow(color: Color.black.opacity(0.15), radius: 6, y: 2)
        .fixedSize()
        .accessibilityElement(children: .contain)
        .accessibilityLabel(
            "Range actions, \(DayStripAccessibility.hourMinuteText(ms: range.startMs)) to \(DayStripAccessibility.hourMinuteText(ms: range.endMs))"
        )
    }

    private func menuAction(_ title: String, destructive: Bool = false, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: .semibold))
            .foregroundStyle(destructive ? Color.scRust : Color.scTeal)
    }

    /// The "unsplit — still searchable" base layer: one full-span rectangle per
    /// recording, titled from the local index (falling back to the directory
    /// name). Task bands overlay these.
    private var stripBaseTracks: [DayStripBaseTrack] {
        spans.map { span in
            DayStripBaseTrack(
                recording: span.name,
                title: index.recordings.first { $0.name == span.name }?.title ?? span.name,
                startMs: span.startMs,
                endMs: span.endMs
            )
        }
    }

    /// N agent/user task bands per recording, from the additive `tasks` field on
    /// `timeline.day` (U9) — see `DayStripSegment.bands(from:)`. A recording with
    /// no tasks contributes nothing here and renders as a plain unsplit base band.
    private var stripSegments: [DayStripSegment] {
        DayStripSegment.bands(from: spans)
    }

    // MARK: - Search (day-scoped)

    /// Anchored search hits inside this day — the amber markers + the header's
    /// "N moments" count.
    private var dayMatchesMs: [Int] {
        // U12: only `.loaded` yields markers. A lapsed user's day-scoped search
        // resolves to `.subscriptionRequired` (recall gated) → no markers, while
        // the day timeline itself (browse verb `timeline.day`) stays fully
        // available. Graceful degradation of the in-day search overlay; the
        // primary upgrade CTA lives on the Days + Recall-palette surfaces.
        // TODO(build-verify): confirm the day-scoped search field's placeholder
        // reads acceptably with zero markers when gated (no explicit CTA here).
        guard case .loaded(let results) = searchModel.phase else { return [] }
        return results.items.compactMap { item in
            // R13 edge: an unanchored hit (a transcript hit whose chunk can't be
            // resolved) falls back to its recording's `startedAt` — a day +
            // startedAt pointer — rather than being dropped.
            let startedAtMs = index.recordings
                .first { $0.name == item.recording }?
                .startedAt.map { Int($0 * 1000) }
            return DaySearchHitResolver.markerMs(
                anchorMs: item.anchorMs,
                recordingStartedAtMs: startedAtMs,
                dayStartMs: dayStartMs,
                dayEndMs: dayEndMs
            )
        }
    }

    /// Manual retry for the daemon-unavailable placeholder: attempt a
    /// stale-daemon restart (fail-safe no-op when the daemon isn't stale), then
    /// reload the day. Guarded against a double-tap racing two loads.
    private func retryDay() {
        guard !reloading else { return }
        reloading = true
        Task {
            await DaemonInstallController.restartStaleDaemonIfNeeded()
            await reloadDay()
            reloading = false
        }
    }

    private func runDayScopedSearch() {
        searchTask?.cancel()
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        searchTask = Task {
            await searchModel.search(text, contentIndexEnabled: contentIndexEnabled)
        }
    }

    // MARK: - Loading

    /// The full day load — the `.task(id:)` entry, the daemon-unavailable retry,
    /// and the post-task-create refresh all route through here. Seeks to the
    /// landing anchor only on the opened day (never after the user navigated to
    /// another date), and clears the highlight the same way.
    private func reloadDay() async {
        await loadDay(
            seekToMs: navigatedAway ? nil : initialSeekMs,
            showLoading: true,
            refreshSettings: true
        )
    }

    /// A quiet live reload (R15/KTD-12): refresh the day's spans + playable
    /// chunks WITHOUT flashing the loading placeholder, preserving the current
    /// playhead so a growing "still recording" strip doesn't jump. Coalesces
    /// overlapping reloads (event bursts) and leaves last-known state on error.
    private func liveReloadDay() async {
        guard !liveReloadInFlight else { return }
        liveReloadInFlight = true
        await loadDay(seekToMs: engine.currentDayMs, showLoading: false, refreshSettings: false)
        liveReloadInFlight = false
    }

    /// - Parameters:
    ///   - seekToMs: the playhead anchor to (re)seek after loading chunks.
    ///   - showLoading: flip to the loading placeholder first (a fresh open);
    ///     `false` keeps the current strip visible for a live refresh.
    ///   - refreshSettings: re-read daemon settings (content-index gate + chunk
    ///     duration); skipped on live reloads — they don't change mid-recording.
    private func loadDay(seekToMs: Int?, showLoading: Bool, refreshSettings: Bool) async {
        if showLoading { loadPhase = .loading }
        if refreshSettings,
           let data = try? await CLIClient.runJSONRaw(["settings", "--json"]),
           let env = try? JSONDecoder().decode(SettingsEnvelope.self, from: data) {
            contentIndexEnabled = env.settings.contentIndexEnabled ?? false
            if let dur = env.settings.chunkDuration { searchModel.chunkDurationSeconds = dur }
        }

        let dayStart = Calendar.current.startOfDay(for: currentDate)
        let request = TimelineDayRequest(
            date: Self.wireDateFormatter.string(from: dayStart),
            tzOffsetSeconds: TimeZone.current.secondsFromGMT(for: dayStart)
        )
        do {
            let response = try await DaemonClient.timelineDay(request)
            spans = response.recordings
            storeMounted = response.storeMounted
            coverageComplete = response.coverageComplete
            loadPhase = .ready
        } catch {
            // A live reload leaves last-known state untouched on a transient
            // daemon blip. A fresh open claims nothing (the strip's load gate
            // keys off `loadPhase != .ready`), so `storeMounted` is left as-is.
            if showLoading {
                spans = []
                loadPhase = .daemonUnavailable
                narrativeState = .hidden
            }
            return
        }

        // Build the seek map off the main actor: manifests + frame lists +
        // legacy recording.db anchors, per recording (KTD-12 direct-read).
        let inputs = spans.map { span -> (name: String, startedAtMs: Int?, durationMs: Int?) in
            let summary = index.recordings.first { $0.name == span.name }
            return (
                name: span.name,
                startedAtMs: summary?.startedAt.map { Int($0 * 1000) },
                durationMs: summary?.durationSeconds.map { Int($0 * 1000) }
            )
        }
        let chunks = await Task.detached(priority: .userInitiated) { () -> [DayPlayableChunk] in
            let root = AppPaths.recordingsRoot
            return inputs.flatMap { input in
                DayMediaLoader.loadChunks(
                    root: root,
                    recording: input.name,
                    startedAtMs: input.startedAtMs,
                    durationMs: input.durationMs
                )
            }
        }.value
        engine.load(chunks: chunks, seekToMs: seekToMs)
        // U8 (F1/R8/R9) — compose the day narrative from each recording's
        // per-recording narrative. Runs after the strip is ready so prose never
        // gates the timeline; refreshed on every load (incl. quiet live reloads)
        // so a live day's narrative fills in as it is written (R10).
        await loadNarrative(for: spans)
    }

    /// Compose the day's narrative section state (U8). Queries `day.narrative` for
    /// each recording on the day and composes their prose into ONE section
    /// (partial on a mixed day, KTD-10). `hasBlocks` — whether any recording
    /// carries a real consolidated block (a task row with a `block_id`, or the
    /// live open block) — distinguishes the "still composing" state (blocks but no
    /// narrative yet) from "nothing to say" (no blocks). Fail-open: a per-recording
    /// miss is skipped, and a total miss with no blocks simply hides the section.
    private func loadNarrative(for spans: [DaySegmentRecording]) async {
        var responses: [DayNarrativeResponse] = []
        for span in spans {
            if let response = try? await DaemonClient.dayNarrative(
                DayNarrativeRequest(recording: span.name)
            ) {
                responses.append(response)
            }
        }
        let hasBlocks = spans.contains { span in
            span.tasks.contains { $0.blockId != nil || $0.isOpen }
        }
        narrativeState = DayNarrativeComposition.compose(responses: responses, hasBlocks: hasBlocks)
    }

    /// R15/KTD-12 — the live-refresh subscription. Rides the EXISTING daemon
    /// event stream (`DaemonClient.subscribe`): a recording event on the opened
    /// day quietly reloads the page. Only recording lifecycle/footage events
    /// while viewing today trigger a reload (`DayLiveRefresh`), so past days —
    /// which are immutable — never churn. A dropped stream (daemon restart)
    /// reconnects after a short backoff; cancelled on disappear.
    private func runLiveRefreshSubscription() async {
        while !Task.isCancelled {
            do {
                for try await event in DaemonClient.subscribe() {
                    if Task.isCancelled { return }
                    guard DayLiveRefresh.shouldReload(
                        eventType: event.type,
                        isViewingToday: Calendar.current.isDateInToday(currentDate)
                    ) else { continue }
                    await liveReloadDay()
                }
            } catch {
                // A dropped subscription is non-fatal — fall through to backoff
                // and reconnect; the strip keeps its last-known state meanwhile.
            }
            if Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
    }

    // MARK: - Formatters

    private static let headerDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.dateFormat = "EEEE, d MMMM"
        return f
    }()

    private static let wireDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}

/// SCR-297 — give the playback pane the footage's aspect ratio, or leave it
/// alone.
///
/// A modifier rather than an inline `.aspectRatio(aspect ?? …)` because the nil
/// case must apply *no* constraint at all, not a neutral-looking one: any
/// placeholder ratio would be a guess, and the whole point is that the pane
/// falls back to its previous fill-the-box behaviour when it does not know the
/// shape.
///
/// Internal rather than nested-and-private so `DayPlaybackPaneLayoutTests` can
/// compose the real modifier stack and check that it produces the geometry
/// `PlaybackAspect.fittedSize` promises. The modifier order in `playbackPane`
/// is the whole design — constraint inside the overlays, overlays inside the
/// flexible slot — and nothing else would catch it silently regressing.
struct SourceAspectFit: ViewModifier {
    let aspect: CGFloat?

    func body(content: Content) -> some View {
        if let aspect {
            content.aspectRatio(aspect, contentMode: .fit)
        } else {
            content
        }
    }
}

/// The confirmed two-endpoint selection awaiting a label (SCR-214 U11). Absolute
/// unix ms; `recording` is resolved from the span midpoint's base track.
private struct PendingTaskLabel: Identifiable {
    let id = UUID()
    let recording: String
    let startMs: Int
    let endMs: Int
}

/// U11 — a range resolved to a SINGLE recording (KTD-8), awaiting Clip/Share
/// consent. Absolute unix ms; `recording` is the one recording the range sits in.
private struct ClipRangeConfirmState: Identifiable {
    let id = UUID()
    let recording: String
    let startMs: Int
    let endMs: Int
    let intent: ClipRangeIntent

    /// "HH:mm–HH:mm" over the range — shown on the consent sheet (never a
    /// recording name, R5).
    var rangeClockText: String {
        "\(ClipsModel.clock(startMs))–\(ClipsModel.clock(endMs))"
    }
}

/// Label-entry sheet for a retroactively-marked task span (SCR-214 U11, AE3).
/// Shows the selected wall-clock window and takes a non-empty name for
/// `tasks.create`.
private struct MarkTaskLabelSheet: View {
    let startMs: Int
    let endMs: Int
    var onSave: (String) -> Void
    var onCancel: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @FocusState private var fieldFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Name this task")
                .font(.title2.weight(.semibold))
            Text("\(Self.clock(startMs)) – \(Self.clock(endMs)) · marked from the day timeline")
                .font(.callout)
                .foregroundStyle(.secondary)
            TextField("Task name", text: $draft)
                .textFieldStyle(.roundedBorder)
                .focused($fieldFocused)
                .onSubmit(submit)
            HStack {
                Button("Cancel") { onCancel(); dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Create task", action: submit)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(minWidth: 420)
        .onAppear { fieldFocused = true }
    }

    private func submit() {
        let name = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        onSave(name)
        dismiss()
    }

    private static func clock(_ ms: Int) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }
}

/// U4 (R16) — pure day-stepping math for the prev/next chevrons, factored out so
/// the month/year/DST-boundary behavior is unit-testable without a view.
enum DayNavigation {
    /// The start-of-day `delta` whole days from `date` on the local calendar.
    /// Uses `Calendar` date arithmetic so month, year, and DST boundaries are
    /// handled correctly (a naive `± 86_400s` would drift on 23h/25h days).
    static func adjacentDay(to date: Date, delta: Int, calendar: Calendar = .current) -> Date {
        let start = calendar.startOfDay(for: date)
        let stepped = calendar.date(byAdding: .day, value: delta, to: start) ?? start
        return calendar.startOfDay(for: stepped)
    }
}

/// U4 (R15/KTD-12) — which streamed daemon events should reload an open day
/// page, pure so the trigger set is testable without a live socket.
enum DayLiveRefresh {
    /// Recording lifecycle / footage events that change what an open day page
    /// should show — capture started/stopped or a new chunk flushed to disk.
    /// Non-recording events (permission, capture-health, backfill/upload
    /// progress, heartbeats) never trigger a reload.
    static let reloadTriggerEventTypes: Set<String> = [
        "started",
        "chunk_finalized",
        "recording_finalized",
        "recording_failed",
        "stopped",
    ]

    /// Whether a streamed event should reload the day. Only recording
    /// lifecycle/footage events, and only while viewing today — past days are
    /// immutable, so a live event never changes them (the "only-if-viewing-
    /// today" guard, KTD-12).
    static func shouldReload(eventType: String, isViewingToday: Bool) -> Bool {
        isViewingToday && reloadTriggerEventTypes.contains(eventType)
    }
}

/// U4 (R13 edge) — resolve a day-scoped search/citation hit to a within-day
/// marker ms. Pure so the unanchored-hit fallback is testable.
enum DaySearchHitResolver {
    /// Prefer the hit's own anchor; when it has none (a transcript hit whose
    /// chunk couldn't be resolved) fall back to the recording's `startedAt` — a
    /// day + startedAt pointer — so the hit is placed at the recording's start
    /// rather than dropped. Returns nil only when neither is available or the
    /// resolved ms falls outside the day's `[startMs, endMs)`.
    static func markerMs(
        anchorMs: Int?,
        recordingStartedAtMs: Int?,
        dayStartMs: Int,
        dayEndMs: Int
    ) -> Int? {
        guard let ms = anchorMs ?? recordingStartedAtMs else { return nil }
        guard ms >= dayStartMs, ms < dayEndMs else { return nil }
        return ms
    }
}
