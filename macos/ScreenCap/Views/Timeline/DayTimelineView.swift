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
    }

    /// Bridges `dayTasks.writeError` to an `isPresented` binding for the
    /// retroactive create path (mirrors the day surfaces. write-error surfacing).
    private var markTaskErrorPresented: Binding<Bool> {
        Binding(
            get: { dayTasks.writeError != nil },
            set: { if !$0 { dayTasks.dismissWriteError() } }
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
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(Color.scCanvas)
        .overlay(alignment: .bottom) { Divider().overlay(Color.scBorderWarm) }
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
                .frame(minWidth: 150, alignment: .leading)

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
        .frame(width: 300)
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
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottomLeading) { timestampChip }
        .overlay(alignment: .bottomTrailing) { if !clipMode { actionButtons } }
        .overlay(alignment: .bottom) { clipBoundsOverlay }
    }

    private func placeholderCaption(_ reason: DayPlaceholderReason) -> String {
        switch loadPhase {
        case .loading: return "loading day…"
        case .daemonUnavailable: return "day view needs the background helper — start ScreenCap's helper and retry"
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

    // U7 exposes these three DayTimelineView-owned hooks for the range action
    // menu. U11 fills Clip/Share (via `/v0/clip.create` + the share sheet) and
    // U9 fills Delete (the confirm sheet + the `delete.start` job). For now they
    // are no-ops that KEEP the selection so the next unit builds on the anchored
    // menu — deletion and clip export are deliberately NOT implemented here.
    // (Named `on…Range` to stay distinct from the SCR-219 `clipRange` state.)
    private func onClipRange(_ range: (startMs: Int, endMs: Int)) {
        // TODO(U11): cut the range into a Clip via clip.create.
    }

    private func onShareRange(_ range: (startMs: Int, endMs: Int)) {
        // TODO(U11): clip the range, then present the share sheet.
    }

    private func onDeleteRange(_ range: (startMs: Int, endMs: Int)) {
        // TODO(U9): open the delete confirm sheet (rounded extent, no-undo,
        // this-Mac-only, kept clips) and drive the delete.start job.
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

/// The confirmed two-endpoint selection awaiting a label (SCR-214 U11). Absolute
/// unix ms; `recording` is resolved from the span midpoint's base track.
private struct PendingTaskLabel: Identifiable {
    let id = UUID()
    let recording: String
    let startMs: Int
    let endMs: Int
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
