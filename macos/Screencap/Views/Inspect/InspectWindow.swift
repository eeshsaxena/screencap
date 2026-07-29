import SwiftUI

/// Stable scene id for the per-recording inspect `WindowGroup`. Shared by the
/// scene declaration in `ScreencapApp` and the openers/callsites so producer
/// and consumers can't drift (mirrors `ReviewWindowID`).
let InspectWindowID = "inspect"

/// Whether the inspect window's "Share / Upload…" hand-off is reachable.
///
/// Extracted as pure logic so the enabled-only-when-ready rule is unit-testable
/// without a SwiftUI render (mirrors `SearchSeek` / `TimelinePaneScrub`). The
/// hand-off opens the consent window for the recording, so it must be reachable
/// only once the recording has loaded — never during preparing/failed, where a
/// rapid click could open the upload flow for a recording that just failed to
/// load locally (R9).
enum InspectShareAffordance {
    static func isEnabled(for state: InspectState) -> Bool {
        if case .ready = state { return true }
        return false
    }

    /// Whether the share-LINK actions (copy / revoke) belong in the menu at all
    /// (SCR-299 R11, AE3).
    ///
    /// A share is built from the recording's cloud copy, so a local-only
    /// recording has nothing to link to. The actions are omitted rather than
    /// shown-and-failing — and this is deliberately narrower than
    /// `isEnabled` above, which still gates the untouched upload hand-off: a
    /// local-only recording must keep its route to the consent window even
    /// though it can't be shared by link.
    ///
    /// `summary` is nil when the recordings index hasn't resolved this
    /// recording yet; treat that as not-yet-shareable rather than guessing.
    static func showsLinkActions(for summary: RecordingSummary?) -> Bool {
        summary?.isShareable == true
    }
}

/// Per-recording read-only inspect window — the "just looking" surface. A
/// `WindowGroup` keyed on the recording name materializes a fresh window for
/// every `openWindow(id: InspectWindowID, value: name)` call, so multiple
/// recordings can be inspected side-by-side (R7).
///
/// It reuses the playback core (video + scrub timeline + moment-anchored event
/// content) but carries NONE of the consent machinery: no Upload row, no masked
/// "what uploads" view, no redaction/coverage evidence. The only bridge to the
/// upload world is a single low-emphasis "Share / Upload…" toolbar item that
/// hands off to the untouched review/consent window (R9/R10).
struct InspectWindow: View {
    let recordingName: String
    @StateObject private var model: InspectWindowViewModel
    /// Share-by-link state for this recording (SCR-299). Owned per window, like
    /// the playback model — two inspect windows share nothing.
    @StateObject private var shareLink: ShareLinkController

    // Observed so a freshly-set `pendingSeekMs[recordingName]` (a second anchored
    // search result for an already-open, already-ready window) re-renders the body
    // and fires the reuse `.onChange` below (Finding #2).
    @ObservedObject private var opener = InspectWindowOpener.shared

    /// The recordings list the app already keeps, injected by the scene. Read
    /// only for this recording's cloud-copy state — inspect still performs no
    /// upload itself.
    @EnvironmentObject private var recordingsIndex: RecordingsIndex

    /// Non-nil while the OS share sheet is presenting the link.
    @State private var sharingURL: URL?
    /// Drives the post-copy confirmation carrying the honesty copy.
    @State private var justCopied = false
    /// Gates the irreversible revoke behind an explicit confirmation.
    @State private var confirmingRevoke = false

    @Environment(\.openWindow) private var openWindow

    @State private var videoModel: VideoPlayerPaneModel?
    @State private var timelineEvents: [TimelineEvent] = []
    @State private var currentTime: Double = 0
    @State private var timelineLoaded = false
    // The "what was recorded" digest, rebuilt from the completed event array
    // (KTD1) — never gated on the `.ready` transition. `eventsParsed` flips true
    // once the detached parse lands, so the pane can distinguish "still reading"
    // from "genuinely empty" (KTD7).
    @State private var recordedSummary: RecordedSummary = .empty
    @State private var eventsParsed = false

    init(recordingName: String) {
        self.recordingName = recordingName
        _model = StateObject(wrappedValue: InspectWindowViewModel(recordingName: recordingName))
        _shareLink = StateObject(wrappedValue: ShareLinkController(recordingName: recordingName))
    }

    var body: some View {
        content
            .frame(minWidth: 720, minHeight: 480)
            // The window title is the recording name so two inspect windows are
            // distinguishable in the title bar, Dock, and Cmd-` cycling (R7).
            .navigationTitle(recordingName)
            .toolbar { shareToolbarItem }
            // Anchor for the OS share sheet; zero-size until `sharingURL` is set.
            .background(ShareServicePresenter(item: $sharingURL))
            .alert("Share link copied", isPresented: $justCopied) {
                copiedConfirmationButtons
            } message: {
                Text(
                    """
                    Anyone with this link can view the recording — it carries \
                    the decryption key, so treat the link itself as a secret.

                    Revoking stops future views, but can't retract a copy \
                    someone has already downloaded.
                    """
                )
            }
            .confirmationDialog(
                "Revoke this share link?",
                isPresented: $confirmingRevoke,
                titleVisibility: .visible
            ) {
                Button("Revoke Link", role: .destructive) {
                    Task { await shareLink.revoke() }
                }
                Button("Keep Link", role: .cancel) {}
            } message: {
                Text(
                    """
                    Anyone using this link will lose access, and it can't be \
                    undone — sharing again means creating a new link.

                    This still can't retract a copy someone has already \
                    downloaded.
                    """
                )
            }
            .alert(
                failureTitle,
                isPresented: Binding(
                    get: { failureCopy != nil },
                    set: { if !$0 { Task { await shareLink.dismissFailure() } } }
                )
            ) {
                if let failureCopy { failureButtons(for: failureCopy) }
            } message: {
                // Static copy keyed on the daemon's error code — never the
                // backend's own text (R13).
                Text(failureCopy?.message ?? "")
            }
            .task { await model.loadInspectData() }
            // Independent of inspect-data: resolves whether this Mac already
            // holds a live link, so Revoke is offered without a reopen (R14).
            .task { await shareLink.refresh() }
            .onChange(of: model.state) { newState in
                buildVideoIfReady(newState)
            }
            // Reuse hook: an already-ready window gets a NEW search moment. The
            // first-open `.ready` path runs only once (gated on `videoModel == nil`),
            // so without this a re-opened window would stay at the prior moment
            // (Finding #2). When the player isn't built yet, leave the entry for
            // `buildVideoIfReady` to consume on `.ready` — no double-consumption,
            // since consuming clears the entry.
            .onChange(of: opener.pendingSeekMs[recordingName]) { seekMs in
                guard seekMs != nil,
                      let vm = videoModel,
                      case .ready(let data) = model.state else { return }
                consumePendingSeek(into: vm, data: data)
            }
    }

    /// This recording's row in the list the app already keeps. Nil until the
    /// index resolves it; the link actions treat that as not-yet-shareable.
    private var recordingSummary: RecordingSummary? {
        recordingsIndex.recordings.first { $0.name == recordingName }
    }

    private var failureCopy: ShareLinkErrorCopy? {
        if case .failed(let copy, _) = shareLink.phase { return copy }
        return nil
    }

    /// Titled by what actually failed — a failed revoke must not claim the app
    /// couldn't create a link.
    private var failureTitle: String {
        if case .failed(_, let operation) = shareLink.phase, operation == .revoke {
            return "Couldn't revoke the share link"
        }
        return "Couldn't create the share link"
    }

    /// Low-emphasis hand-off to the consent window (R9/R10), now also the home
    /// of the share-link actions (SCR-299). A secondary toolbar item — never a
    /// prominent/primary button — enabled only once the recording has loaded.
    ///
    /// The upload hand-off is unchanged and stays FIRST: it reaches the
    /// untouched review/consent window exactly as before, and remains available
    /// for a local-only recording that has no link to copy. Inspect still
    /// performs no upload itself and carries no consent machinery — signing in
    /// routes out to the main window rather than presenting an account sheet
    /// here.
    private var shareToolbarItem: some ToolbarContent {
        ToolbarItem(placement: .automatic) {
            Menu {
                Button("Upload…") {
                    ReviewWindowOpener.shared.open(recordingName: recordingName)
                }

                if InspectShareAffordance.showsLinkActions(for: recordingSummary) {
                    Divider()
                    // Read before acting: the one-line truth about what the
                    // link is. The full statement — including that revoking
                    // can't retract an already-downloaded copy — follows on
                    // the confirmation (R7).
                    Text("Link is view-only and carries its own key")
                    // Keyed on whether the link is still in memory, NOT on
                    // whether a share exists. The key is never persisted (it
                    // only ever lives in the fragment), so after a relaunch the
                    // old link is unrecoverable and copying genuinely mints a
                    // new one — the label must not call that "again".
                    if shareLink.lastCreatedURL != nil {
                        Button("Copy Link Again") {
                            shareLink.copyExistingLink()
                            justCopied = true
                        }
                    } else {
                        Button("Copy Share Link") {
                            Task {
                                await shareLink.copyLink()
                                if case .hasLink = shareLink.phase { justCopied = true }
                            }
                        }
                    }
                    if shareLink.activeToken != nil {
                        // Confirmed rather than immediate: revoking cannot be
                        // undone, and it cuts off everyone already holding the
                        // link with no warning to them.
                        Button("Revoke Share Link…") { confirmingRevoke = true }
                    }
                }
            } label: {
                Label("Share / Upload…", systemImage: "square.and.arrow.up")
            }
            .help("Upload this recording, or copy a view-only share link")
            .disabled(!InspectShareAffordance.isEnabled(for: model.state) || shareLink.isBusy)
        }
    }

    /// Post-copy confirmation. This is where the honest description of the
    /// security model lands in full (R7, and origin assumptions A2/A5): the
    /// link carries the key, anyone holding it can view, and revoking cannot
    /// retract a copy already fetched.
    @ViewBuilder
    private var copiedConfirmationButtons: some View {
        if shareLink.lastCreatedURL != nil {
            Button("Share…") { sharingURL = shareLink.lastCreatedURL }
        }
        Button("Done", role: .cancel) {}
    }

    @ViewBuilder
    private func failureButtons(for copy: ShareLinkErrorCopy) -> some View {
        switch copy.action {
        case .retry:
            // Routed by the recorded operation, so retrying a failed revoke
            // retries the revoke rather than minting a fresh link.
            Button("Try Again") { Task { await shareLink.retryFailedOperation() } }
        case .signIn:
            // Inspect deliberately holds no account sheet, so sign-in routes to
            // the main window, where the account surface lives.
            Button("Open Screencap") { openWindow(id: MainWindowID) }
        case .noRecovery:
            EmptyView()
        }
        Button("OK", role: .cancel) { Task { await shareLink.dismissFailure() } }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .preparing:
            preparingState
        case .failed(let message):
            failedState(message: message)
        case .ready(let data):
            readyPanes(data: data)
        }
    }

    private var preparingState: some View {
        // No scrub runs behind inspect-data, so an indeterminate spinner with a
        // plain label is the honest interstitial — no progress detail or ETA.
        VStack(spacing: 12) {
            ProgressView()
            Text("Loading…")
                .font(.body)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// Friendly failure body for a non-technical operator (A1): a plain headline
    /// with the raw error tucked into a disclosure, plus a Try-Again that
    /// re-loads in place (the window stays open rather than vanishing).
    private func failedState(message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text("Could not load this recording.")
                .font(.headline)
                .multilineTextAlignment(.center)
            DisclosureGroup("Details") {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .frame(maxWidth: 380, alignment: .leading)
            }
            .frame(maxWidth: 360)
            Button("Try Again") {
                Task { await model.loadInspectData() }
            }
            .keyboardShortcut(.defaultAction)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }

    @ViewBuilder
    private func readyPanes(data: InspectData) -> some View {
        if let videoModel {
            VStack(spacing: 0) {
                // Renders only for a transient lock / corrupt DB; a benign
                // event-free recording reads cleanly and shows nothing here.
                TimingUnavailableCallout(status: data.timingStatus)
                // The real local video is the primary surface (no masked
                // "truth view" — that is an upload concept).
                VideoPlayerPane(model: videoModel)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .onReceive(videoModel.$currentTime) { t in currentTime = t }
                Divider()
                // Moment-anchored captured content (its own empty state covers a
                // recording with no events); selecting a row seeks the video.
                EventContentPane(
                    events: timelineEvents,
                    currentTime: currentTime
                ) { seconds in
                    videoModel.seek(toSeconds: seconds)
                }
                .frame(height: 132)
                Divider()
                // Event-tick scrub timeline — no risky-band / redaction-marker
                // overlays (those are upload-review concerns; the defaults are
                // empty, so omitting them draws ticks + cursor only).
                TimelinePane(
                    events: timelineEvents,
                    durationSeconds: data.durationSeconds,
                    currentTime: currentTime
                ) { seconds in
                    videoModel.seek(toSeconds: seconds)
                }
                .frame(height: 80)
                Divider()
                // Collapsed-by-default "what was recorded" digest (R1/R2). Built
                // from the completed event array, so it shows a loading placeholder
                // until the detached parse lands, then the digest or honest empty
                // state — readiness never waits on it.
                RecordedSummaryPane(
                    summary: recordedSummary,
                    isParsing: !eventsParsed
                )
            }
        } else {
            preparingState
        }
    }

    /// On first `.ready`, build the AVKit-backed playback model and, if this
    /// window was opened from a search result, seek once to the hit moment
    /// (SCR-174). With no pending seek (Recordings-list open, or an unanchored
    /// search result) the video simply opens at the start, paused (R6).
    private func buildVideoIfReady(_ newState: InspectState) {
        guard case .ready(let data) = newState, videoModel == nil else { return }
        let engine = LiveVideoPlaybackEngine(url: data.videoURL)
        let vm = VideoPlayerPaneModel(engine: engine)
        videoModel = vm

        consumePendingSeek(into: vm, data: data)

        if !timelineLoaded {
            timelineLoaded = true
            // Parse the full local event set off the main actor (a long
            // recording's events.jsonl can be large), then fold the digest from
            // the completed array on the same detached task (pure, O(N)) so the
            // main actor only takes the finished values. Recomputing here — where
            // `timelineEvents` is produced — rather than on the `.ready`
            // transition keeps the digest built from the whole array (KTD1).
            Task.detached(priority: .userInitiated) {
                let parsed = TimelineEventParser.parse(
                    urls: data.eventsURLs,
                    recordingStartedAt: data.startedAt
                )
                let summary = RecordedSummaryBuilder.build(
                    events: parsed,
                    blockedIntervals: data.blockedIntervals,
                    protectedIntervals: data.protectedIntervals
                )
                await MainActor.run {
                    timelineEvents = parsed
                    recordedSummary = summary
                    eventsParsed = true
                }
            }
        }
    }

    /// Read-and-clear the pending seek for this recording and, if one was set,
    /// seek `vm` to that moment. Clearing the entry is the double-consumption
    /// guard: the first-open path (`buildVideoIfReady`) and the reuse `.onChange`
    /// hook share this helper, so whichever runs first leaves nothing for the
    /// other. With no pending seek the player simply stays where it is — a
    /// Recordings-list reuse sets `pendingSeekMs[name] = nil` at the click site,
    /// so it never jumps to a stale moment.
    private func consumePendingSeek(into vm: VideoPlayerPaneModel, data: InspectData) {
        guard let seekMs = InspectWindowOpener.shared.pendingSeekMs[recordingName] else { return }
        InspectWindowOpener.shared.pendingSeekMs[recordingName] = nil
        vm.seek(toSeconds: SearchSeek.relativeSeconds(
            anchorMs: seekMs,
            startedAt: data.startedAt,
            durationSeconds: data.durationSeconds
        ))
    }
}
