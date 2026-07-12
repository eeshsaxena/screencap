import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Stable scene id for the per-recording review `WindowGroup`. Shared by the
/// scene declaration in `ScreenCapApp` and the Library card's "Review &
/// upload…" callsite so producer and consumers can't drift.
let ReviewWindowID = "review"

/// Per-recording review window (plan U3 scaffolded, U8 composed). A
/// `WindowGroup` scene keyed on the recording name materializes a fresh
/// window for every `openWindow(id: ReviewWindowID, value: name)` call —
/// satisfying R3 (multiple concurrent windows). The singleton `Window`
/// used by the main scene is the wrong primitive here; SCR-55 documents
/// why the two scene types are not interchangeable.
///
/// Owns a `ReviewWindowViewModel` for the entire `preparing → ready →
/// uploading → succeeded / failed` lifecycle. The video pane (U5) and the
/// timeline pane (U6) render side-by-side once the U2 envelope arrives;
/// the bottom action row swaps between Upload/Cancel, progress, success
/// confirmation, and Retry depending on the state.
struct ReviewWindow: View {
    let recordingName: String
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var auth: CloudAuthController
    @EnvironmentObject private var uploads: UploadCoordinator
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model: ReviewWindowViewModel
    /// SCR-219 (U6): the local-file clip export. Owned here (not on the
    /// viewmodel) because clip export is a distinct terminal action that bypasses
    /// the entire upload machinery — no `AccountSheetPolicy`, no `UploadRegistry`,
    /// no account (KTD5). Its published `state` drives a determinate progress
    /// modal that is decoupled from the encode (never welded to an `await`).
    @StateObject private var clipExport = ClipExportController()
    /// Presents the clip-export progress/result modal. A plain `@State` bool the
    /// modal binds to — the modal's CONTENT switches on `clipExport.state`, so
    /// presentation stays decoupled from the long encode.
    @State private var showClipExportSheet = false
    /// The chosen output path, retained so a retry (after a retryable
    /// `clip_busy`) re-exports to the same destination without re-prompting.
    @State private var pendingClipOutPath: String?

    @State private var videoModel: VideoPlayerPaneModel?
    @State private var timelineEvents: [TimelineEvent] = []
    @State private var screenshots: [ReviewScreenshot] = []
    // Derived once on `.ready` — these depend only on the (fixed) redaction
    // evidence, not on currentTime, so recomputing them on every 10Hz playback
    // tick would re-sort identical data.
    @State private var redactionMarkers: [Double] = []
    @State private var riskyIntervals: [TimelineInterval] = []
    @State private var currentTime: Double = 0
    @State private var timelineLoaded = false
    /// Drives the Account & Plan sheet in `.upload` context (plan U6,
    /// account-sheet U5/R14). Shown when Upload is tapped while signed out
    /// (sign-in framing) or while signed in on Local Pro (upgrade-to-Cloud
    /// framing), instead of letting the CLI refuse opaquely.
    @State private var showAccountSheet = false
    /// Tracks whether this window has incremented the upload coordinator's
    /// active-upload count, so Sign Out stays disabled for exactly the span of
    /// this window's upload and the count is balanced on close.
    @State private var countedUpload = false
    /// Tracks whether THIS window initiated the in-flight sign-in flow. The auth
    /// controller is app-wide, so only the window that started the flow may
    /// cancel it on close/dismiss — otherwise closing window B would abort a
    /// login that window A started.
    @State private var startedSignIn = false

    init(recordingName: String) {
        self.recordingName = recordingName
        _model = StateObject(wrappedValue: ReviewWindowViewModel(
            recordingName: recordingName,
            effects: LiveReviewWindowEffects()
        ))
    }

    var body: some View {
        VStack(spacing: 0) {
            content
            Divider()
            bottomActions
        }
        .frame(minWidth: 720, minHeight: 540)
        .navigationTitle(recordingName)
        .onAppear {
            // Forward the dismiss action so the auto-close timer can fire it.
            // Set on the StateObject-preserved viewmodel rather than a
            // separate forwarder struct so SwiftUI's View-struct churn
            // doesn't replace the closure the viewmodel actually holds.
            model.dismissHandler = { dismiss() }
        }
        .task {
            await model.loadReviewData()
        }
        .onChange(of: model.state) { newState in
            if case .ready(let data) = newState, videoModel == nil {
                let engine = LiveVideoPlaybackEngine(url: data.videoURL)
                videoModel = VideoPlayerPaneModel(engine: engine)
                // SCR-174 (U6): if this window was opened from a search result,
                // seek to the hit moment (consumed one-shot). A null/0 startedAt
                // falls back to the start; if the recording isn't playable the
                // window simply opens without seeking (graceful fallback).
                if let seekMs = ReviewWindowOpener.shared.pendingSeekMs[recordingName] {
                    ReviewWindowOpener.shared.pendingSeekMs[recordingName] = nil
                    videoModel?.seek(toSeconds: SearchSeek.relativeSeconds(
                        anchorMs: seekMs,
                        startedAt: data.startedAt,
                        durationSeconds: data.durationSeconds
                    ))
                }
                // The masked screenshots that actually upload — the primary
                // truth view (U6). Parsed once on first ready.
                screenshots = ScreenshotTruth.screenshots(
                    from: data.screenshotURLs,
                    startedAt: data.startedAt
                )
                // Redaction markers + risky-moment bands depend only on the
                // fixed redaction evidence, so derive them once here rather
                // than on every playback tick.
                redactionMarkers = RedactionTimeline.relativeMarkers(
                    data.redaction, startedAt: data.startedAt
                )
                riskyIntervals = RedactionTimeline.riskyIntervals(
                    data.redaction, startedAt: data.startedAt,
                    duration: data.durationSeconds
                )
                if !timelineLoaded {
                    timelineLoaded = true
                    Task.detached(priority: .userInitiated) {
                        // Parse the FULL scrubbed event set (all per-chunk files),
                        // not just events_paths[0], so the timeline + content view
                        // reflect every event that uploads (reviewed == uploaded).
                        let parsed = TimelineEventParser.parse(
                            urls: data.eventsURLs,
                            recordingStartedAt: data.startedAt
                        )
                        await MainActor.run { timelineEvents = parsed }
                    }
                }
            }
            syncUploadCount(for: newState)
        }
        .sheet(isPresented: $showAccountSheet, onDismiss: {
            // Esc / system / external (menu) dismissal bypasses the Close
            // button's onDismiss closure, so run the same ownership-aware
            // teardown here. Converges every dismissal route on one place.
            teardownSignInIfOwned()
        }) {
            AccountSheetView(
                auth: auth,
                context: .upload,
                onStartSignIn: {
                    // Ownership latch (R14): fired precisely when THIS sheet's
                    // own Sign In / retry buttons launch a login (the retired
                    // SignInPromptView.onStartSignIn pattern), so closing this
                    // window cancels only flows it actually started — never a
                    // sign-in another surface began while this sheet happened
                    // to be up.
                    startedSignIn = true
                },
                onSettled: {
                    // Fires exactly once, on the signed-out → signed-in
                    // transition only (the sheet's one-shot latch) — dismiss
                    // and auto-start the gated upload (R14). The signed-in
                    // Local Pro presentation never settles (no transition), so
                    // it can never auto-fire an upload the signer would refuse.
                    showAccountSheet = false
                    model.startUpload()
                },
                onDismiss: {
                    // Closing the sheet via its own Close sets isPresented
                    // false, which fires the .sheet(onDismiss:) teardown above —
                    // so this only needs to dismiss; cancel happens there.
                    showAccountSheet = false
                }
            )
            // Interactive-dismissal blocking while the browser round-trip is
            // live is owned by AccountSheetView itself (it applies
            // `.interactiveDismissDisabled` while signInFlow is in progress).
        }
        .sheet(isPresented: $showClipExportSheet) {
            clipExportSheet
                // Block interactive dismissal while the encode runs — Cancel is
                // the only way out mid-export, so a stray Esc/drag can't strand
                // an orphaned subprocess. Terminal states (succeeded / failed /
                // cancelled) re-enable dismissal.
                .interactiveDismissDisabled(clipExportIsRunning)
        }
        .onDisappear {
            // Window-close-as-cancel for an in-flight clip export: SIGTERM the
            // child (U1's atomic write leaves no partial file). Safe-on-idle.
            clipExport.cancel()
            model.windowDidClose()
            // Balance the active-upload count if the window closes mid-upload
            // (onChange won't fire after the view is gone).
            if countedUpload {
                countedUpload = false
                uploads.uploadDidFinish()
            }
            // Closing the window mid-sign-in should cancel the login this window
            // started — otherwise the subprocess orphans until the 180s timeout
            // and its captured completion could fire startUpload() on a
            // torn-down window. Ownership-guarded so we don't abort a flow
            // another window started on the shared controller.
            teardownSignInIfOwned()
        }
    }

    /// True while this window's sign-in sheet has a `login` round-trip running.
    private var isSignInInProgress: Bool {
        if case .inProgress = auth.signInFlow { return true }
        return false
    }

    /// Cancels the in-flight sign-in only if THIS window started it and a flow
    /// is still in progress. Safe to call from any dismissal route (sheet
    /// onDismiss, window onDisappear); clears the ownership flag so it's a no-op
    /// on a second call.
    private func teardownSignInIfOwned() {
        guard startedSignIn else { return }
        startedSignIn = false
        if isSignInInProgress {
            auth.cancelSignIn()
        }
    }

    /// Upload tapped (the `.ready` and `.failed`-with-retry action). Cloud-
    /// entitled and signed in → start the upload; signed out, signed in on
    /// Local Pro, or definitively lapsed with the paywall on (R14 — today's
    /// raw signer-refusal paths) → present the Account & Plan sheet in
    /// `.upload` context rather than letting `screencap upload` refuse
    /// opaquely (plan U6 / account-sheet U5).
    /// The pure decision lives in `AccountSheetPolicy.uploadEntryAction`.
    ///
    /// `refreshIfNeeded` first, because sign-in state is now resolved lazily
    /// rather than at app launch (SCR-241): if the user opens a review window
    /// and hits Upload before any cloud surface has resolved `status`, it would
    /// still be `.unknown` (→ `isSignedIn == false`) and a genuinely signed-in
    /// user would be wrongly shown the sign-in sheet. Resolving here — on an
    /// explicit cloud action — is exactly where decrypting the Keychain (and any
    /// prompt) belongs. No-ops once resolved, so it adds no cost on later taps.
    private func attemptUpload() {
        Task {
            await auth.refreshIfNeeded()
            switch AccountSheetPolicy.uploadEntryAction(
                isSignedIn: auth.isSignedIn,
                tier: auth.tier,
                trialState: auth.trialState,
                paywallEnabled: auth.paywallEnabled
            ) {
            case .proceed:
                model.startUpload()
            case .presentAccountSheet:
                showAccountSheet = true
            }
        }
    }

    /// Keeps the auth controller's active-upload count in lockstep with this
    /// window's upload lifecycle so Sign Out is disabled only while an upload
    /// is actually in flight (design-review state c).
    private func syncUploadCount(for state: ReviewState) {
        let uploading: Bool
        if case .uploading = state { uploading = true } else { uploading = false }
        if uploading && !countedUpload {
            countedUpload = true
            uploads.uploadDidStart()
        } else if !uploading && countedUpload {
            countedUpload = false
            uploads.uploadDidFinish()
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .preparing:
            preparingState
        case .failed(let message, let retryData) where retryData == nil:
            // Preparation failure (U2 envelope error) → no panes to render.
            // Upload-time failures fall through to the panes-with-error-row
            // layout below.
            failedPreparationState(message: message)
        default:
            panesIfAvailable
        }
    }

    private var preparingState: some View {
        VStack(spacing: 12) {
            // Indeterminate spinner — the scrubber exposes no progress callback,
            // so the copy sets the expectation instead (R4). The honest framing
            // ("what will upload") tells the operator the wait is the scrub that
            // produces exactly the bytes they're about to review.
            ProgressView()
            Text("Preparing what will upload…")
                .font(.body)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func failedPreparationState(message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text("Couldn't prepare a safe version for review. Nothing was uploaded.")
                .font(.headline)
                .multilineTextAlignment(.center)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal)
            Button("Try Again") {
                Task { await model.loadReviewData() }
            }
            .keyboardShortcut(.defaultAction)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var panesIfAvailable: some View {
        if let videoModel, let data = currentData() {
            VStack(spacing: 0) {
                // Non-blocking advisory (SCR-107/SCR-166): the recording's
                // timing metadata couldn't be placed — a transient lock or an
                // unreadable recording.db — so the timeline can't be drawn.
                // Surfaced ABOVE the redaction evidence because it's a more
                // fundamental data-integrity signal — the video below still plays.
                TimingUnavailableCallout(status: data.timingStatus)
                // Per-recording redaction summary, framed as protection (R8),
                // with the distinct fail-closed callout (R14) separate beneath.
                RedactionEvidenceView(redaction: data.redaction)
                FailClosedCallout(redaction: data.redaction)
                // SCR-219 (U6, KTD4): for a clip export the video LEAVES to
                // external recipients and — unlike these masked screenshots — is
                // only capture-blocked, not text-masked. Surface that prominently,
                // adjacent to the preview frames. Gated on the envelope's
                // `clip_video_capture_blocked_only` honesty flag.
                if data.clipVideoCaptureBlockedOnly {
                    clipHonestyNote
                }
                Divider()
                // The masked-screenshot truth view is the PRIMARY surface — it
                // shows what actually uploads (R15). The local video beside it
                // is a secondary navigation aid that never uploads, labeled as
                // such so the operator can't mistake it for the payload.
                HStack(spacing: 0) {
                    VStack(spacing: 0) {
                        ScreenshotTruthPane(
                            screenshots: screenshots,
                            currentTime: currentTime
                        )
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        // Persistent coverage disclosure beneath the truth view
                        // (R9): the allowed-app on-screen-PII blind spot is the
                        // one fact requiring operator action.
                        CoverageStrip(coverage: data.coverage)
                    }
                    Divider()
                    localVideoPane(videoModel, isClipExport: data.clipVideoCaptureBlockedOnly)
                        .frame(width: 280)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                Divider()
                // Moment-anchored captured content from the scrubbed copy (U7);
                // selecting a row seeks the visual to that moment.
                EventContentPane(
                    events: timelineEvents,
                    currentTime: currentTime
                ) { seconds in
                    videoModel.seek(toSeconds: seconds)
                }
                .frame(height: 132)
                Divider()
                TimelinePane(
                    events: timelineEvents,
                    durationSeconds: data.durationSeconds,
                    currentTime: currentTime,
                    riskyIntervals: riskyIntervals,
                    redactionMarkers: redactionMarkers
                ) { seconds in
                    videoModel.seek(toSeconds: seconds)
                }
                .frame(height: 80)
            }
        } else {
            preparingState
        }
    }

    /// The local navigation video. For a whole-recording review it carries a
    /// persistent "not uploaded" label so the operator never mistakes it for the
    /// payload (R15). For a CLIP export (KTD4) that label is FALSE — the video is
    /// exactly what leaves — so it is overridden to name the clip as the payload.
    private func localVideoPane(_ videoModel: VideoPlayerPaneModel, isClipExport: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: isClipExport ? "square.and.arrow.up" : "play.rectangle")
                    .foregroundStyle(isClipExport ? Color.orange : Color.secondary)
                Text(isClipExport
                    ? "Clip preview — this video will be exported"
                    : "Local preview — not uploaded")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(isClipExport ? Color.orange : Color.secondary)
                Spacer()
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background((isClipExport ? Color.orange : Color.secondary).opacity(0.12))
            Divider()
            VideoPlayerPane(model: videoModel)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .onReceive(videoModel.$currentTime) { t in
                    currentTime = t
                }
        }
    }

    // MARK: - Clip export (SCR-219 U6)

    /// The prominent, honest note the clip consent surface shows adjacent to the
    /// preview frames (KTD4): the exported video is capture-blocked but NOT
    /// text-masked and carries the original, unredacted audio, and it leaves to
    /// external recipients.
    private var clipHonestyNote: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text("The exported clip's video is not text-masked")
                    .font(.callout.weight(.semibold))
                Text("These preview frames are redacted, but the exported video "
                    + "shows on-screen text that is not masked and includes the "
                    + "original audio. Share this clip only with people you'd show "
                    + "your screen to.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12))
    }

    /// True while the clip export subprocess is running (drives the modal's
    /// interactive-dismissal block).
    private var clipExportIsRunning: Bool {
        if case .exporting = clipExport.state { return true }
        return false
    }

    /// The clip-export progress/result modal. Its content switches on
    /// `clipExport.state`, so the modal is driven by published state changes and
    /// is NOT welded to any `await` on the long encode (decoupling per the
    /// HUD-teardown learning). Determinate progress (R10) + a Cancel that aborts
    /// the encode; success offers reveal-in-Finder; failures are typed/retryable.
    @ViewBuilder
    private var clipExportSheet: some View {
        VStack(spacing: 16) {
            switch clipExport.state {
            case .exporting(let progress):
                VStack(spacing: 12) {
                    if progress.framesTotal > 0 {
                        ProgressView(value: progress.fraction) {
                            Text("Exporting clip…").font(.headline)
                        }
                        .frame(width: 260)
                        Text("\(progress.framesDone) of \(progress.framesTotal) frames")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        ProgressView("Preparing clip…")
                            .controlSize(.small)
                    }
                    Button("Cancel") {
                        // Aborts the encode (SIGTERM). U1's atomic .tmp/os.replace
                        // means no partial or delivered file — the modal dismisses
                        // off the resulting `.cancelled` state below.
                        clipExport.cancel()
                    }
                    .keyboardShortcut(.cancelAction)
                }
            case .succeeded(let path):
                VStack(spacing: 12) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .font(.largeTitle)
                    Text("Clip exported").font(.headline)
                    Text(URL(fileURLWithPath: path).lastPathComponent)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    HStack {
                        Button("Show in Finder") { revealInFinder(path) }
                        Button("Done") { showClipExportSheet = false }
                            .keyboardShortcut(.defaultAction)
                            .buttonStyle(.borderedProminent)
                    }
                }
            case .failed(let failure):
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.octagon.fill")
                        .foregroundStyle(.red)
                        .font(.largeTitle)
                    Text(failure.message)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button("Close") { showClipExportSheet = false }
                        if failure.retryable {
                            Button("Try Again") { retryClipExport() }
                                .keyboardShortcut(.defaultAction)
                                .buttonStyle(.borderedProminent)
                        }
                    }
                }
            case .idle, .cancelled:
                // Transient — dismissed by the onChange below.
                ProgressView().controlSize(.small)
            }
        }
        .padding(24)
        .frame(minWidth: 340)
        .onChange(of: clipExport.state) { newState in
            // A cancel aborts to `.cancelled`; dismiss the modal so the review
            // window is usable again. Success/failure keep the modal up (they
            // offer reveal / retry / close affordances).
            if case .cancelled = newState { showClipExportSheet = false }
        }
    }

    /// "Export clip" tapped: pick a destination, then spawn `screencap clip` via
    /// `ClipExportController`. Deliberately does NOT call `attemptUpload` — no
    /// account, no `AccountSheetPolicy`, no keychain prompt (KTD5).
    private func beginClipExport() {
        guard let range = model.clipRange else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.mpeg4Movie]
        panel.nameFieldStringValue = suggestedClipFilename()
        panel.canCreateDirectories = true
        panel.title = "Export clip"
        panel.prompt = "Export"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        pendingClipOutPath = url.path
        showClipExportSheet = true
        clipExport.start(name: recordingName, range: range, outPath: url.path)
    }

    /// Retry a retryable failure (a transient `clip_busy`) against the same range
    /// and destination the user already chose.
    private func retryClipExport() {
        guard let range = model.clipRange, let out = pendingClipOutPath else {
            showClipExportSheet = false
            return
        }
        clipExport.start(name: recordingName, range: range, outPath: out)
    }

    /// Reveal the exported clip in Finder (R5 delivery).
    private func revealInFinder(_ path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    /// Default save-panel filename. A follow-up (per plan Outstanding Questions)
    /// may enrich this with the moment label / timestamp.
    private func suggestedClipFilename() -> String {
        "\(recordingName)-clip.mp4"
    }

    /// The resolved review data for the current state, if any — drives the
    /// panes, the redaction summary, the coverage strip, and the timeline
    /// markers. Available in ready / uploading / failed-with-retry.
    private func currentData() -> ReviewData? {
        model.state.reviewData
    }

    @ViewBuilder
    private var bottomActions: some View {
        switch model.state {
        case .preparing:
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(12)
        case .ready:
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                if model.isClipReview {
                    // SCR-219 (U6): clip export is a LOCAL file write — no
                    // account, no `AccountSheetPolicy` gate (KTD5). `beginClipExport`
                    // goes straight to the save panel + `screencap clip`, never
                    // through `attemptUpload`, so no auth/keychain prompt fires.
                    Button("Export clip") { beginClipExport() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                } else {
                    Button("Upload") { attemptUpload() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                }
            }
            .padding(12)
        case .uploading(let progress, _):
            HStack(spacing: 12) {
                if progress.filesTotal > 0 {
                    ProgressView(value: progress.fraction) {
                        Text("Uploading \(progress.filesDone) of \(progress.filesTotal)…")
                            .font(.caption)
                    }
                    .frame(maxWidth: 320)
                } else {
                    ProgressView("Starting upload…")
                        .controlSize(.small)
                }
                Spacer()
                Button("Cancel") {
                    // Dispatch SIGTERM synchronously on the explicit Cancel
                    // intent rather than deferring to onDisappear — the
                    // teardown path still fires cancel() as a backstop, but
                    // hitting it here means the kill goes out while the
                    // window is still on screen and any kernel queueing for
                    // the dismiss animation can't delay it.
                    model.cancel()
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
            }
            .padding(12)
        case .succeeded(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text("Uploaded \(summary.uploaded) file\(summary.uploaded == 1 ? "" : "s")")
                    .font(.body)
                Spacer()
                Text("Closing…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(12)
        case .failed(let message, let retryData):
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.octagon.fill")
                    .foregroundStyle(.red)
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Spacer()
                Button("Close") { dismiss() }
                if retryData != nil {
                    Button("Retry") { attemptUpload() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                }
            }
            .padding(12)
        case .refused(let message, _):
            // SCR-155: cross-window refusal — framed as info, not an error
            // (the recording isn't broken, another window is uploading it).
            // Deliberately no Retry: while the owner holds the claim a Retry
            // would silently re-refuse, and "released" is optimistic (SCR-154
            // releases eagerly, before the owning child exits). Close is the
            // honest affordance; if the owner doesn't finish, the recording
            // stays `uploaded == false` and the Recordings-list Upload button
            // (intent-agnostic recovery floor) re-uploads it — the copy points
            // there rather than promising completion (SCR-163).
            HStack(spacing: 8) {
                Image(systemName: "info.circle.fill")
                    .foregroundStyle(.secondary)
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(12)
        case .busy(let message, let retryData):
            // SCR-158: busy-lock skip — framed as info, not an error (the child
            // exited 0; the recording isn't broken). Unlike `.refused`, a
            // busy-lock is transient, so a Retry IS offered when there is panes
            // data to re-run against; the default action so ⏎ retries.
            HStack(spacing: 8) {
                Image(systemName: "clock.fill")
                    .foregroundStyle(.secondary)
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Spacer()
                Button("Close") { dismiss() }
                    // Mirror the `.refused`/`.failed` rows: when a Retry is
                    // present it owns ⏎ (`.defaultAction`) and Close takes Esc
                    // (`.cancelAction`); with no Retry, Close is the default
                    // action so ⏎ still dismisses (SCR-158).
                    .keyboardShortcut(retryData != nil ? .cancelAction : .defaultAction)
                if retryData != nil {
                    Button("Retry") { attemptUpload() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                }
            }
            .padding(12)
        }
    }
}

/// Production-side `ReviewWindowEffects` — bridges the viewmodel's
/// refresh / scheduling needs to live AppKit / NotificationCenter APIs.
/// Dismiss is handled directly by the viewmodel's `dismissHandler`
/// (set in `ReviewWindow.onAppear`) so this effects type stays free of
/// SwiftUI environment dependencies.
@MainActor
struct LiveReviewWindowEffects: ReviewWindowEffects {
    func refreshIndex() async {
        // The index instance lives in the SwiftUI environment of the main
        // window. The review window receives it via `.environmentObject`
        // (see ScreenCapApp). Posting a notification keeps the effects
        // surface dependency-free.
        await MainActor.run {
            NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)
        }
    }

    func scheduleAutoClose(after seconds: Double, _ action: @escaping @MainActor () -> Void) -> AutoCloseHandle {
        let task = Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
            if !Task.isCancelled {
                action()
            }
        }
        return AutoCloseHandle { task.cancel() }
    }
}

extension Notification.Name {
    /// Posted by `LiveReviewWindowEffects.refreshIndex()` so the main
    /// window's `RecordingsIndex` can refresh after a successful upload.
    /// Subscribed by the index from inside the main scene's body (added
    /// in U9).
    static let reviewWindowUploadSucceeded = Notification.Name("com.screencap.reviewWindow.uploadSucceeded")
}
