import AVFoundation
import AVKit
import AppKit
import SwiftUI
import UniformTypeIdentifiers

// Moments — the merged surface that replaces the separate Tasks and Clips
// destinations. One cross-day, reverse-chronological list unions the app-detected
// task spans (`tasks.query`, windowed) with the ranges the user clipped
// (`clip.list`, unpaged) interleaved by footage time (KTD-3), marks the clipped
// rows (R3), filters to just those (R5), and keeps a durable clip visible even
// when the app-detected half is empty (Intelligence off) or fails to load
// (R6/R7). A sealed / absent / error vault branches before the empty check
// (KTD-14/KTD-20). No recording name is ever shown (R5).

/// Loads BOTH sources and merges them, mutating the clip catalog. `@MainActor` so
/// `@Published` updates and the NSSavePanel / share presentation stay on the main
/// thread.
@MainActor
final class MomentsController: ObservableObject {
    enum Phase: Equatable {
        case loading
        case loaded(
            storeState: StoreState,
            days: [TasksQueryDay],
            clips: [ClipRecord],
            recordings: [TasksQueryRecordingStatus]
        )
        case failed(message: String)
    }

    @Published private(set) var phase: Phase = .loading
    /// A non-fatal action failure (delete, export) surfaced beside the list rather
    /// than replacing it — an honest error, never a silent no-op.
    @Published var lastActionError: String?
    /// One source failed while the other loaded — surfaced inline so a durable clip
    /// (or a named task) is never hidden by the other half's transient failure
    /// (R6/R7). `nil` when both halves loaded.
    @Published var partialLoadNotice: String?

    /// Whether a first load has settled — drives whether a widen shows the full
    /// loader (only the initial load does; a "Load older" reload stays quiet).
    var isLoaded: Bool {
        if case .loaded = phase { return true }
        return false
    }

    /// Load both sources and merge. Fail-open (R6/R7): the full-screen error fires
    /// ONLY when BOTH halves fail; if either half loaded, its rows render with an
    /// inline notice for the failed half — a transient `tasks.query` failure must
    /// never hide the user's durable clips, and vice versa. A vault-degraded
    /// response is NOT a failure: it lands on `.loaded` with a non-mounted
    /// `storeState` so `StoreStateView` renders (KTD-14).
    func load(windowDays: Int, showLoading: Bool) async {
        if showLoading { phase = .loading }

        // Sequential (both on the main actor): clip list is unpaged (KTD-2), the
        // task query is windowed to `windowDays`.
        let taskResult = await fetchTasks(windowDays: windowDays)
        let clipResult = await fetchClips()

        var taskResp: TasksQueryResponse?
        var clipResp: ClipListResponse?
        if case .success(let resp) = taskResult { taskResp = resp }
        if case .success(let resp) = clipResult { clipResp = resp }

        let taskFailed = taskResp == nil
        let clipFailed = clipResp == nil

        if taskFailed && clipFailed {
            if case .failure(let error) = taskResult {
                phase = .failed(message: Self.loadFailureMessage(error))
            }
            return
        }

        partialLoadNotice = Self.partialNotice(taskFailed: taskFailed, clipFailed: clipFailed)
        phase = .loaded(
            storeState: Self.resolveStoreState(taskResp, clipResp),
            days: taskResp?.days ?? [],
            clips: clipResp?.clips ?? [],
            recordings: taskResp?.recordings ?? []
        )
    }

    /// Delete a clip (mp4 + catalog entry) then reload both sources. A failure
    /// surfaces on `lastActionError`; the list is left intact.
    func deleteClip(id: String, windowDays: Int) async {
        do {
            _ = try await DaemonClient.clipDelete(id: id)
            await load(windowDays: windowDays, showLoading: false)
        } catch {
            lastActionError = ClipsModel.errorMessage(error)
        }
    }

    // MARK: - Fetch helpers

    private func fetchTasks(windowDays: Int) async -> Result<TasksQueryResponse, Error> {
        let now = Date()
        let calendar = Calendar.current
        let start = calendar.date(byAdding: .day, value: -windowDays, to: calendar.startOfDay(for: now)) ?? now
        let tz = TimeZone.current.secondsFromGMT(for: now)
        do {
            return .success(try await DaemonClient.tasksQuery(
                startDate: TasksModel.dateKey(start, calendar: calendar),
                endDate: TasksModel.dateKey(now, calendar: calendar),
                tzOffsetSeconds: tz
            ))
        } catch {
            return .failure(error)
        }
    }

    private func fetchClips() async -> Result<ClipListResponse, Error> {
        do {
            return .success(try await DaemonClient.clipList())
        } catch {
            return .failure(error)
        }
    }

    // MARK: - Reconciliation

    /// A non-mounted store on either successful response wins (both derive it from
    /// the same vault state), so the surface branches to `StoreStateView`.
    private static func resolveStoreState(_ tasks: TasksQueryResponse?, _ clips: ClipListResponse?) -> StoreState {
        if let tasks, !tasks.resolvedStoreState.isMounted { return tasks.resolvedStoreState }
        if let clips, !clips.resolvedStoreState.isMounted { return clips.resolvedStoreState }
        return .mounted
    }

    private static func partialNotice(taskFailed: Bool, clipFailed: Bool) -> String? {
        if taskFailed && !clipFailed {
            return "Your activity couldn’t load just now — only your clips are shown. Refresh to try again."
        }
        if clipFailed && !taskFailed {
            return "Your clips couldn’t load just now. Refresh to try again."
        }
        return nil
    }

    /// Honest copy for a both-halves-down failure — a daemon-down state reads as
    /// "needs the background helper", everything else falls back to the shared mapper.
    static func loadFailureMessage(_ error: Error) -> String {
        if case DaemonClientError.socketUnavailable = error {
            return "Moments needs the background helper — start Screencap’s helper and try again."
        }
        if case DaemonClientError.connectionFailed = error {
            return "Moments needs the background helper — start Screencap’s helper and try again."
        }
        return ClipsModel.errorMessage(error)
    }
}

struct MomentsView: View {
    @EnvironmentObject private var store: StoreController
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var intelligence: IntelligenceController

    /// A row click opens the day page seeked to the row's span with the band
    /// highlighted (AE3): (day, seekMs, highlight).
    var onOpenTimeline: (Date, Int?, DaySpanHighlight?) -> Void
    /// The honest "set up intelligence" zero state deep-links to the Intelligence
    /// pane (R21) — reached only when there are no clips either (R7/KTD-6).
    var onOpenIntelligence: () -> Void

    @StateObject private var controller = MomentsController()
    /// The shared task-curation write-through (R12) — the SAME cache + verbs the
    /// day page uses, so a rename here and a rename there can't drift.
    @StateObject private var dayTasks = DayTasks()

    @State private var query = ""
    /// The type filter: false = All, true = Clipped-only (R5).
    @State private var clippedOnly = false
    /// The loaded task window in days back from today; "Load older" widens it. All
    /// clips are always loaded (KTD-2), so this paginates only the app-detected half.
    @State private var windowDays = 30
    @State private var loadingOlder = false

    @State private var renameTarget: TasksModel.TaskRow?
    @State private var playingClip: ClipRecord?
    @State private var pendingDelete: ClipRecord?
    @State private var shareURL: URL?

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            .task { await intelligence.refresh() }
            .task(id: windowDays) {
                await controller.load(windowDays: windowDays, showLoading: !controller.isLoaded)
                loadingOlder = false
            }
            .onChange(of: recorder.state) { _ in
                Task { await controller.load(windowDays: windowDays, showLoading: false) }
            }
            .alert(
                "Couldn't update the task",
                isPresented: writeErrorPresented,
                presenting: dayTasks.writeError
            ) { _ in
                Button("Retry") {
                    Task { await dayTasks.retryLastWrite(); await controller.load(windowDays: windowDays, showLoading: false) }
                }
                Button("Dismiss", role: .cancel) { dayTasks.dismissWriteError() }
            } message: { err in
                Text(err.message)
            }
            .alert("Delete this clip?", isPresented: pendingDeletePresented, presenting: pendingDelete) { clip in
                Button("Delete clip", role: .destructive) {
                    let id = clip.id
                    pendingDelete = nil
                    Task { await controller.deleteClip(id: id, windowDays: windowDays) }
                }
                Button("Cancel", role: .cancel) { pendingDelete = nil }
            } message: { _ in
                Text("This can't be undone. The clip is removed from this Mac.")
            }
            .alert("Something went wrong", isPresented: actionErrorPresented, presenting: controller.lastActionError) { _ in
                Button("OK", role: .cancel) { controller.lastActionError = nil }
            } message: { message in
                Text(message)
            }
            .sheet(item: $renameTarget) { row in
                MomentRenameSheet(currentName: row.name) { newName in
                    renameTarget = nil
                    Task {
                        let ok = await dayTasks.rename(recording: row.recording, taskIndex: row.taskIndex, to: newName)
                        if ok { await controller.load(windowDays: windowDays, showLoading: false) }
                    }
                } onCancel: {
                    renameTarget = nil
                }
            }
            .sheet(item: $playingClip) { clip in
                MomentClipPlayerSheet(
                    url: URL(fileURLWithPath: clip.path ?? ""),
                    title: ClipsModel.dayLabel(clip.sourceDay) + " · " + ClipsModel.rangeClockText(clip),
                    onClose: { playingClip = nil }
                )
            }
            .background(alignment: .topLeading) {
                ShareServicePresenter(item: $shareURL)
                    .frame(width: 1, height: 1)
                    .accessibilityHidden(true)
            }
    }

    @ViewBuilder
    private var content: some View {
        switch controller.phase {
        case .loading:
            loadingState
        case .failed(let message):
            failedState(message)
        case .loaded(let storeState, let days, let clips, let recordings):
            if !storeState.isMounted {
                storeStateView(storeState)
            } else {
                populated(days: days, clips: clips, recordings: recordings)
            }
        }
    }

    // MARK: - Populated

    private var verdict: IntelligenceVerdict? {
        IntelligenceVerdict.compose(probe: OnDeviceModelStatus.probe(), settings: intelligence.settings)
    }

    private func listState(
        days: [TasksQueryDay],
        clips: [ClipRecord],
        recordings: [TasksQueryRecordingStatus]
    ) -> MomentsModel.ListState {
        MomentsModel.listState(
            days: days,
            clips: clips,
            recordings: recordings,
            query: query,
            clippedOnly: clippedOnly,
            verdict: verdict
        )
    }

    private func populated(
        days: [TasksQueryDay],
        clips: [ClipRecord],
        recordings: [TasksQueryRecordingStatus]
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.bottom, 18)
            filterControls
                .padding(.bottom, 14)
            if let notice = controller.partialLoadNotice {
                partialBanner(notice)
                    .padding(.bottom, 12)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 22) {
                    switch listState(days: days, clips: clips, recordings: recordings) {
                    case .populated(let groups):
                        ForEach(groups) { group in
                            dayGroupView(group)
                        }
                        // The pager widens only the app-detected window (KTD-2); under
                        // the Clipped filter it would fetch rows the filter hides, so
                        // hide it there (D4).
                        if !clippedOnly {
                            loadOlderButton
                        }
                    case .filterZero(let q, let onlyClipped):
                        filterZeroState(query: q, clippedOnly: onlyClipped)
                    case .systemZero(let zero):
                        systemZeroState(zero)
                    }
                }
                .padding(.bottom, 8)
            }
            .scrollContentBackground(.hidden)
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 28)
    }

    private var header: some View {
        HStack(alignment: .center) {
            Text("Moments")
                .font(SCTypography.screenHeading)
                .foregroundStyle(Color.scInk)
            Spacer()
        }
    }

    private var filterControls: some View {
        HStack(spacing: 12) {
            Picker("", selection: $clippedOnly) {
                Text("All").tag(false)
                Text("Clipped").tag(true)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 170)
            // The substring filter narrows named (app-detected) rows; it doesn't
            // apply under the Clipped filter, so it's hidden there.
            if !clippedOnly {
                filterField
            }
            Spacer(minLength: 0)
        }
    }

    private var filterField: some View {
        HStack(spacing: 8) {
            Image(systemName: "line.3.horizontal.decrease.circle")
                .font(.system(size: 12))
                .foregroundStyle(Color.scInkMuted)
            TextField("Filter by name", text: $query)
                .textFieldStyle(.plain)
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInk)
            if !query.isEmpty {
                Button {
                    query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(Color.scInkFaint)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear filter")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .frame(maxWidth: 320, alignment: .leading)
        .background(Color.scCanvas, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
    }

    private func partialBanner(_ text: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.circle")
                .font(.system(size: 12))
                .foregroundStyle(Color.scAmberText)
            Text(text)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkSecondary)
            Spacer()
            Button("Refresh") {
                Task { await controller.load(windowDays: windowDays, showLoading: false) }
            }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: .medium))
            .foregroundStyle(Color.scTeal)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(Color.scCanvas, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
    }

    @ViewBuilder
    private func dayGroupView(_ group: MomentsModel.MomentDayGroup) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(group.label)
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 8) {
                ForEach(group.rows) { row in
                    MomentRowView(
                        row: row,
                        openDay: { openDay(row) },
                        play: { if case .clipped(let clip) = row { playClip(clip) } },
                        share: { if case .clipped(let clip) = row { shareClip(clip) } },
                        exportCopy: { if case .clipped(let clip) = row { exportCopy(clip) } },
                        deleteClip: { if case .clipped(let clip) = row { pendingDelete = clip } },
                        rename: { if case .auto(let task) = row { renameTarget = task } },
                        deleteTask: { if case .auto(let task) = row { deleteTask(task) } }
                    )
                }
            }
        }
    }

    @ViewBuilder
    private var loadOlderButton: some View {
        HStack {
            Spacer()
            Button {
                loadingOlder = true
                windowDays += 30
            } label: {
                Text(loadingOlder ? "Loading…" : "Load older days")
                    .font(SCTypography.sans(size: 12.5, weight: .medium))
                    .foregroundStyle(Color.scTeal)
            }
            .buttonStyle(.plain)
            .disabled(loadingOlder)
            Spacer()
        }
        .padding(.top, 4)
    }

    // MARK: - Row actions

    /// Jump to the row's source day, seeked to its span (AE3). A clipped row with a
    /// malformed source day isn't day-navigable (it still plays/exports in place).
    private func openDay(_ row: MomentsModel.MomentRow) {
        guard let day = row.day else { return }
        onOpenTimeline(day, row.startMs, row.highlight)
    }

    private func deleteTask(_ task: TasksModel.TaskRow) {
        Task {
            let ok = await dayTasks.delete(recording: task.recording, taskIndex: task.taskIndex)
            if ok { await controller.load(windowDays: windowDays, showLoading: false) }
        }
    }

    private func playClip(_ clip: ClipRecord) {
        guard let path = clip.path, !path.isEmpty,
              FileManager.default.fileExists(atPath: path) else {
            controller.lastActionError = "That clip's file is missing on this Mac."
            return
        }
        playingClip = clip
    }

    private func shareClip(_ clip: ClipRecord) {
        guard let path = clip.path, !path.isEmpty,
              FileManager.default.fileExists(atPath: path) else {
            controller.lastActionError = "That clip's file is missing on this Mac."
            return
        }
        shareURL = URL(fileURLWithPath: path)
    }

    private func exportCopy(_ clip: ClipRecord) {
        guard let path = clip.path, !path.isEmpty else {
            controller.lastActionError = "That clip's file is missing on this Mac."
            return
        }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.mpeg4Movie]
        panel.nameFieldStringValue = suggestedFilename(clip)
        panel.canCreateDirectories = true
        panel.title = "Export a copy"
        panel.prompt = "Export"
        guard panel.runModal() == .OK, let dest = panel.url else { return }
        do {
            if FileManager.default.fileExists(atPath: dest.path) {
                try FileManager.default.removeItem(at: dest)
            }
            try FileManager.default.copyItem(at: URL(fileURLWithPath: path), to: dest)
        } catch {
            controller.lastActionError = "Couldn't export a copy. \(error.localizedDescription)"
        }
    }

    private func suggestedFilename(_ clip: ClipRecord) -> String {
        let range = "\(ClipsModel.clock(clip.startMs))-\(ClipsModel.clock(clip.endMs))"
            .replacingOccurrences(of: ":", with: "")
        return "clip-\(clip.sourceDay)-\(range).mp4"
    }

    // MARK: - Zero / degraded states

    private func filterZeroState(query q: String, clippedOnly onlyClipped: Bool) -> some View {
        let title = onlyClipped ? "No clipped moments yet" : "No matches for “\(q)”"
        let message = onlyClipped
            ? "Clip a range on a day to keep a moment here. Switch to All to see your activity."
            : "Try a different word, or clear the filter to see everything."
        let clearLabel = onlyClipped ? "Show all" : "Clear filter"
        return VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInkSecondary)
            Text(message)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
            Button(clearLabel) { query = ""; clippedOnly = false }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12.5, weight: .medium))
                .foregroundStyle(Color.scTeal)
                .padding(.top, 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 6)
    }

    private func systemZeroState(_ zero: TasksModel.SystemZero) -> some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: zero.offersSetup ? "sparkles" : "tray")
                .font(.system(size: 30))
                .foregroundStyle(Color.scInkMuted)
            Text(zero.title)
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
            Text(zero.message)
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            if zero.offersSetup {
                Button("Set up intelligence") { onOpenIntelligence() }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.scTeal)
                    .padding(.top, 2)
            } else if zero.isRetryable {
                Button("Refresh") { Task { await controller.load(windowDays: windowDays, showLoading: true) } }
                    .buttonStyle(.bordered)
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.top, 24)
        .padding(SCMetrics.space4)
    }

    private func storeStateView(_ storeState: StoreState) -> some View {
        StoreStateView(
            storeState: storeState,
            onUnlock: { store.unlock() },
            onRetry: { Task { await controller.load(windowDays: windowDays, showLoading: true) } },
            onSetup: { store.initializeStore() },
            isBusy: store.phase != .idle,
            errorText: store.lastError
        )
    }

    private var loadingState: some View {
        VStack(spacing: SCMetrics.space3) {
            ProgressView().controlSize(.large)
            Text("Loading moments…")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func failedState(_ message: String) -> some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 34))
                .foregroundStyle(Color.scErrorFg)
            Text("Couldn't load moments")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(message)
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button("Retry") { Task { await controller.load(windowDays: windowDays, showLoading: true) } }
                .padding(.top, SCMetrics.space1)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    // MARK: - Bindings

    private var writeErrorPresented: Binding<Bool> {
        Binding(get: { dayTasks.writeError != nil }, set: { if !$0 { dayTasks.dismissWriteError() } })
    }

    private var pendingDeletePresented: Binding<Bool> {
        Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })
    }

    private var actionErrorPresented: Binding<Bool> {
        Binding(get: { controller.lastActionError != nil }, set: { if !$0 { controller.lastActionError = nil } })
    }
}

// MARK: - Rename sheet

/// A minimal rename sheet for an app-detected task (R12). Split / merge stay on the
/// day page, where the timeline gives the split point and multi-select their context.
private struct MomentRenameSheet: View {
    let currentName: String
    var onSave: (String) -> Void
    var onCancel: () -> Void

    @State private var name: String

    init(currentName: String, onSave: @escaping (String) -> Void, onCancel: @escaping () -> Void) {
        self.currentName = currentName
        self.onSave = onSave
        self.onCancel = onCancel
        _name = State(initialValue: currentName)
    }

    private var trimmed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename task")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            TextField("Task name", text: $name)
                .textFieldStyle(.roundedBorder)
                .font(SCTypography.sans(size: 13))
                .onSubmit { if !trimmed.isEmpty { onSave(trimmed) } }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel, action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button("Save") { onSave(trimmed) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(trimmed.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 360)
    }
}

// MARK: - Clip player sheet

/// Local playback of a clip in a sheet (never uploads — a same-EUID local file).
/// Owns its own `AVPlayer`, torn down on disappear (mirrors the Clips surface).
private struct MomentClipPlayerSheet: View {
    let url: URL
    let title: String
    var onClose: () -> Void

    @State private var player = AVPlayer()

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title)
                    .font(SCTypography.sans(size: 13, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Spacer()
                Button("Done") { onClose() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(12)
            AVPlayerNSView(player: player)
                .frame(minWidth: 640, minHeight: 380)
        }
        .frame(minWidth: 640, minHeight: 420)
        .onAppear {
            player.replaceCurrentItem(with: AVPlayerItem(url: url))
            player.play()
        }
        .onDisappear {
            player.pause()
            player.replaceCurrentItem(with: nil)
        }
    }
}
