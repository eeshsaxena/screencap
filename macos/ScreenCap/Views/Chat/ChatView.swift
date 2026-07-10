import AppKit
import SwiftUI

// Conversational-recall U8 — the multi-turn Chat destination (KTD7). A scrolling
// transcript of grounded answers, each with a collapsible sources strip that
// REUSES Search's shipped result components (`SnippetHighlighter`,
// `RecordingCardThumbnail`) and the deep-link opener (`InspectWindowOpener`).
// Selecting a source opens the Inspect window at that exact moment.
//
// Interaction states this surface commits to (U8 Approach):
//  - In-flight answer: a thinking indicator while generating (whole-render after
//    completion — the daemon returns the whole answer; streaming would hook in at
//    `ChatTurn.State`).
//  - Sources: a collapsible strip BELOW each turn (scales across turns better
//    than a side panel).
//  - Composer: empty input disables send; in-flight locks input + shows progress;
//    a failed turn shows a retry that preserves prior turns.
//  - Empty / first-run: an "ask about your recorded history" prompt with example
//    questions and the on-device-by-default posture shown at rest.
//  - Coverage / consent: the honest coverage state renders inline on the relevant
//    turn; an OCR-off / not-indexed coverage surfaces the one-time consent
//    affordance, re-running that turn on consent.

struct ChatView: View {
    /// Navigate to the Intelligence settings pane — the no-backend affordance's CTA
    /// (R6). Wired by `MainWindow` to its `ShellRoute` (`route = .intelligence`); a
    /// no-op default keeps previews and standalone instantiation working.
    var onOpenIntelligenceSettings: () -> Void = {}
    @StateObject private var model = ChatViewModel()
    /// Recall-consent state surfacing (KTD1) — the existing recall row governs
    /// cloud use; on-device is the default and shown at rest.
    @EnvironmentObject private var intelligence: IntelligenceController
    /// Resolves a source pointer's recording → a `RecordingSummary` so the
    /// sources strip can reuse `RecordingCardThumbnail` (no new thumbnail path).
    @EnvironmentObject private var index: RecordingsIndex

    @State private var draft = ""
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()
    /// The composer field focus, so first-run lands the caret in the input.
    @FocusState private var composerFocused: Bool
    /// Forward-only OCR-indexing consent state (R13). Read once from settings;
    /// `enableOcrConsent` flips it and re-runs the thin-evidence turn.
    @State private var contentIndexEnabled = false

    var body: some View {
        VStack(spacing: 0) {
            header
            transcript
            composer
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Color.scCanvas)
        .task {
            await intelligence.refresh()
            await loadSettings()
            composerFocused = true
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("Chat")
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
            Text("ask about your recorded history")
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInkMuted)
            Spacer(minLength: 0)
            providerBadge
        }
        .padding(.horizontal, SCMetrics.space6)
        .padding(.top, SCMetrics.space6)
        .padding(.bottom, SCMetrics.space4)
    }

    /// The on-device-by-default posture, shown at rest (R7). A cloud provider only
    /// answers content questions when the recall consent row is on (KTD1).
    private var providerBadge: some View {
        let onDevice = (intelligence.settings?.provider ?? "on-device") == "on-device"
        return HStack(spacing: 6) {
            Circle().fill(onDevice ? Color.scTeal : Color.scInkFaint).frame(width: 7, height: 7)
            Text(onDevice ? "on-device · nothing leaves this Mac" : "cloud provider connected")
                .font(SCTypography.mono(size: 11))
                .foregroundStyle(Color.scInkMuted)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: - Transcript

    @ViewBuilder
    private var transcript: some View {
        if model.turns.isEmpty {
            emptyState
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: SCMetrics.space5) {
                        ForEach(model.turns) { turn in
                            ChatTurnView(
                                turn: turn,
                                queryTerms: terms(turn.question),
                                selected: SelectedIntelligence(
                                    provider: intelligence.settings?.provider ?? "on-device"
                                ),
                                index: index,
                                frameIndex: frameIndex,
                                thumbnailLoader: thumbnailLoader,
                                showOcrConsent: turn.answer?.suggestsOcrConsent == true && !contentIndexEnabled,
                                onOpenSource: openSource,
                                onRetry: { Task { await model.retry(turnID: turn.id) } },
                                onEnableOcrConsent: { Task { await enableOcrConsent(turnID: turn.id) } },
                                onOpenIntelligenceSettings: onOpenIntelligenceSettings,
                                onOpenSystemSettings: openAppleIntelligenceSettings
                            )
                            .id(turn.id)
                        }
                    }
                    .padding(.horizontal, SCMetrics.space6)
                    .padding(.vertical, SCMetrics.space4)
                }
                .onChange(of: model.turns.count) { _ in
                    // Keep the newest turn in view as the conversation grows.
                    if let last = model.turns.last?.id {
                        withAnimation { proxy.scrollTo(last, anchor: .bottom) }
                    }
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: SCMetrics.space4) {
            Spacer(minLength: 0)
            Text("Ask about your recorded history")
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
            Text("Get a written answer grounded in the real moments you recorded — with the moments it drew from shown as sources. It runs on this Mac by default.")
                .font(SCTypography.sans(size: 13.5))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 440, alignment: .leading)
            VStack(alignment: .leading, spacing: SCMetrics.space2) {
                ForEach(Self.exampleQuestions, id: \.self) { example in
                    Button {
                        draft = example
                        Task { await submit() }
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "sparkles")
                                .font(.system(size: 11))
                                .foregroundStyle(Color.scTeal)
                            Text(example)
                                .font(SCTypography.sans(size: 13))
                                .foregroundStyle(Color.scInk)
                        }
                        .padding(.horizontal, SCMetrics.space3)
                        .padding(.vertical, 9)
                        .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusInner))
                        .overlay(
                            RoundedRectangle(cornerRadius: SCMetrics.radiusInner)
                                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                        )
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, SCMetrics.space2)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .padding(.horizontal, SCMetrics.space6)
    }

    /// Example questions mirroring F1 (point lookup) and F2 (period summary).
    static let exampleQuestions = [
        "What was that vendor-portal refund error around 2pm yesterday?",
        "How much time did I spend in Salesforce this morning?",
        "Recap what I worked on today.",
    ]

    // MARK: - Composer

    private var composer: some View {
        VStack(spacing: 0) {
            Divider().overlay(Color.scBorderWarm)
            HStack(alignment: .bottom, spacing: SCMetrics.space2) {
                TextField("Ask about a moment or a stretch of time…", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(SCTypography.sans(size: 14))
                    .foregroundStyle(Color.scInk)
                    .lineLimit(1...5)
                    .focused($composerFocused)
                    .disabled(model.isLoading)
                    .onSubmit { Task { await submit() } }
                    .accessibilityLabel("Ask about your recorded history. Answers only from what's on this Mac.")
                sendButton
            }
            .padding(.horizontal, SCMetrics.space5)
            .padding(.vertical, SCMetrics.space3)
        }
        .background(Color.scSurface)
    }

    @ViewBuilder
    private var sendButton: some View {
        if model.isLoading {
            ProgressView()
                .controlSize(.small)
                .frame(width: 30, height: 24)
                .accessibilityLabel("Generating answer")
        } else {
            Button {
                Task { await submit() }
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 22))
                    .foregroundStyle(model.canSend(draft) ? Color.scTeal : Color.scInkFaint)
            }
            .buttonStyle(.plain)
            .disabled(!model.canSend(draft))
            .keyboardShortcut(.return, modifiers: [])
            .accessibilityLabel("Send")
        }
    }

    // MARK: - Actions

    private func submit() async {
        let question = draft
        guard model.canSend(question) else { return }
        draft = ""
        await model.send(question)
        composerFocused = true
    }

    /// A source tap → open the Inspect window at that exact moment, mirroring the
    /// Library/palette deep-link (set `pendingSeekMs` then open, keyed by name).
    private func openSource(_ source: ChatSource) {
        InspectWindowOpener.shared.pendingSeekMs[source.recording] = source.timestampMs
        InspectWindowOpener.shared.open(recordingName: source.recording)
    }

    private func terms(_ question: String) -> [String] {
        question.split(whereSeparator: { $0.isWhitespace || $0.isPunctuation })
            .map(String.init)
            .filter { $0.count > 2 }
    }

    /// Open System Settings at the Apple Intelligence & Siri pane — the on-device
    /// no-backend affordance's CTA (R6). The on-device model is gated by the
    /// system-wide Apple Intelligence switch, which the app can't flip; deep-link to
    /// it instead. Falls back to opening System Settings at its root if the OS
    /// doesn't recognize the anchor (mirrors `PermissionController`'s open-with-
    /// fallback).
    private func openAppleIntelligenceSettings() {
        let anchors = [
            "x-apple.systempreferences:com.apple.Siri-Settings.extension",
            "x-apple.systempreferences:",
        ]
        for raw in anchors {
            if let url = URL(string: raw), NSWorkspace.shared.open(url) { return }
        }
    }

    // MARK: - Settings / OCR consent (R13, mirrors RecallPaletteView)

    private func loadSettings() async {
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            contentIndexEnabled = env.settings.contentIndexEnabled ?? false
        } catch {
            contentIndexEnabled = false
        }
    }

    /// Enable on-screen-text indexing going forward (forward-only), then re-run
    /// the thin-evidence turn — optimistic with revert-on-failure (the shipped
    /// enableConsent pattern).
    private func enableOcrConsent(turnID: ChatTurn.ID) async {
        contentIndexEnabled = true
        do {
            _ = try await CLIClient.runJSONRaw(
                ["settings", "--set", "content_index_enabled=true", "--json"]
            )
        } catch {
            contentIndexEnabled = false
            return
        }
        await model.retry(turnID: turnID)
    }
}

// MARK: - One turn

/// A single conversation turn: the question bubble, the answer prose (or thinking
/// indicator / failure), a collapsible sources strip, and inline coverage/consent.
private struct ChatTurnView: View {
    let turn: ChatTurn
    let queryTerms: [String]
    /// The selected intelligence model, so the no-backend affordance can name the
    /// on-device (Apple Intelligence) system step vs the generic both-paths copy.
    let selected: SelectedIntelligence
    let index: RecordingsIndex
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    let showOcrConsent: Bool
    var onOpenSource: (ChatSource) -> Void
    var onRetry: () -> Void
    var onEnableOcrConsent: () -> Void
    var onOpenIntelligenceSettings: () -> Void
    var onOpenSystemSettings: () -> Void

    @State private var sourcesExpanded = true

    var body: some View {
        VStack(alignment: .leading, spacing: SCMetrics.space3) {
            questionBubble
            answerBody
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var questionBubble: some View {
        HStack {
            Spacer(minLength: 40)
            Text(turn.question)
                .font(SCTypography.sans(size: 13.5))
                .foregroundStyle(Color.scInk)
                .padding(.horizontal, SCMetrics.space3)
                .padding(.vertical, 9)
                .background(Color.scTeal.opacity(0.1), in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
                .frame(maxWidth: 520, alignment: .trailing)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("You asked: \(turn.question)")
    }

    @ViewBuilder
    private var answerBody: some View {
        switch turn.state {
        case .loading:
            thinkingIndicator
        case .answered(let answer):
            answeredContent(answer)
        case .failed(let message):
            failureRow(message)
        }
    }

    private var thinkingIndicator: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("Looking through your recorded history…")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
        }
        .padding(.vertical, 4)
        .accessibilityLabel("Generating an answer")
    }

    @ViewBuilder
    private func answeredContent(_ answer: ChatAnswer) -> some View {
        VStack(alignment: .leading, spacing: SCMetrics.space3) {
            // The answer prose — REUSES SnippetHighlighter to emphasize the
            // question's terms inside the generated answer.
            Text(SnippetHighlighter.attributed(answer.text, terms: queryTerms))
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(answer.refusal ? Color.scInkSecondary : Color.scInk)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .frame(maxWidth: 640, alignment: .leading)

            // The no-backend state gets its own affordance (a distinct, actionable
            // card), NOT the muted single-line coverage note used for every other
            // state — so "no AI is available" never reads like "nothing matched".
            if answer.honestState == .noBackend {
                noBackendAffordance
            } else {
                coverageLine(answer)
            }

            if showOcrConsent {
                ocrConsentBanner
            }

            if !answer.sources.isEmpty {
                sourcesStrip(answer.sources)
            }
        }
    }

    /// The transparent no-backend affordance (R5/R6): no AI model is available, so
    /// explain the path(s) forward and link out. Visually distinct from the
    /// single-line `coverageLine` (a card with a call-to-action). Copy is
    /// selected-model- and OS-aware but never hides a path and never nudges: when
    /// the on-device model is picked on a capable OS, the block is the system-wide
    /// Apple Intelligence switch, so it names that step and deep-links to it.
    private var noBackendAffordance: some View {
        let guidance = NoBackendGuidance.make(selected: selected, canRunOnDevice: Self.osCanRunOnDevice)
        return VStack(alignment: .leading, spacing: 8) {
            Text(guidance.title)
                .font(SCTypography.sans(size: 13, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Text(guidance.body)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            // When Apple Intelligence is the block, System Settings is the primary
            // action and Intelligence Settings stays reachable as the secondary
            // path; otherwise Intelligence Settings is the sole primary action.
            // (The two button styles are distinct types, so branch the views rather
            // than ternary the style.)
            HStack(spacing: 8) {
                if guidance.showsSystemSettings {
                    Button("Open System Settings", action: onOpenSystemSettings)
                        .buttonStyle(.borderedProminent)
                        .tint(Color.scTeal)
                    Button("Open Intelligence Settings", action: onOpenIntelligenceSettings)
                        .buttonStyle(.bordered)
                        .tint(Color.scTeal)
                } else {
                    Button("Open Intelligence Settings", action: onOpenIntelligenceSettings)
                        .buttonStyle(.borderedProminent)
                        .tint(Color.scTeal)
                }
            }
        }
        .padding(SCMetrics.space3)
        .frame(maxWidth: 640, alignment: .leading)
        .background(Color.scTealSoft.opacity(0.4), in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        // Do NOT `.accessibilityElement(children: .combine)` here: it would fold the
        // Buttons into one static element and make the CTAs unreachable by VoiceOver.
        // The Text views read out on their own; each Button keeps its own focusable
        // element (mirrors ocrConsentBanner).
    }

    /// Whether this OS can run Apple Foundation Models at all (macOS 26+). Isolated
    /// so `NoBackendGuidance.make` stays a pure, fully-tested function and this
    /// single `#available` line is the only untestable-in-CI bit.
    static var osCanRunOnDevice: Bool {
        if #available(macOS 26.0, *) { return true } else { return false }
    }

    /// The honest coverage state, inline on this turn (R12). Only surfaced when it
    /// says something beyond "ok" — a clean answer doesn't need a caption.
    @ViewBuilder
    private func coverageLine(_ answer: ChatAnswer) -> some View {
        if answer.coverage.state != .ok, !answer.coverage.note.isEmpty {
            HStack(alignment: .top, spacing: 6) {
                Image(systemName: "info.circle")
                    .font(.system(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                Text(answer.coverage.note)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: 640, alignment: .leading)
        }
    }

    /// The one-time OCR-consent affordance, attached inline to THIS thin-evidence
    /// turn (R13). Enabling re-runs the turn.
    private var ocrConsentBanner: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Search on-screen text too?")
                .font(SCTypography.sans(size: 13, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Text("Turn this on to also draw on the text that was on your screen. Newly recorded screens become answerable — it all stays on this Mac and is never uploaded.")
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Button("Turn on", action: onEnableOcrConsent)
                .buttonStyle(.borderedProminent)
                .tint(Color.scTeal)
        }
        .padding(SCMetrics.space3)
        .frame(maxWidth: 640, alignment: .leading)
        .background(Color.scTealSoft.opacity(0.4), in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
    }

    // MARK: - Sources strip (collapsible)

    @ViewBuilder
    private func sourcesStrip(_ sources: [ChatSource]) -> some View {
        VStack(alignment: .leading, spacing: SCMetrics.space2) {
            Button {
                sourcesExpanded.toggle()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: sourcesExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                    Text(sources.count == 1 ? "1 source" : "\(sources.count) sources")
                        .font(SCTypography.mono(size: 11))
                    Spacer(minLength: 0)
                }
                .foregroundStyle(Color.scInkMuted)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(sourcesExpanded ? "Hide sources" : "Show \(sources.count) sources")

            if sourcesExpanded {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: SCMetrics.space2) {
                        ForEach(sources) { source in
                            ChatSourceCard(
                                source: source,
                                summary: index.recordings.first { $0.name == source.recording },
                                frameIndex: frameIndex,
                                thumbnailLoader: thumbnailLoader,
                                onOpen: { onOpenSource(source) }
                            )
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .padding(.top, 2)
    }

    // MARK: - Failure

    private func failureRow(_ message: String) -> some View {
        HStack(alignment: .top, spacing: SCMetrics.space2) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 12))
                .foregroundStyle(Color.scInkMuted)
            VStack(alignment: .leading, spacing: 6) {
                Text(message)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Retry", action: onRetry)
                    .font(SCTypography.sans(size: 12))
            }
        }
        .frame(maxWidth: 640, alignment: .leading)
        .padding(SCMetrics.space3)
        .background(Color.scErrorSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
    }
}

// MARK: - One source card

/// One source in the strip — REUSES `RecordingCardThumbnail` for the recording
/// thumbnail (when a `RecordingSummary` is resolvable) and deep-links via the tap.
/// A pointer whose recording isn't in the index falls back to the pointer
/// thumbnail resolver so the moment is still previewed.
private struct ChatSourceCard: View {
    let source: ChatSource
    let summary: RecordingSummary?
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    var onOpen: () -> Void

    @State private var pointerImage: ThumbnailImage?

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 6) {
                thumbnail
                    .frame(width: 150)
                Text(streamLabel)
                    .font(SCTypography.mono(size: 9.5))
                    .foregroundStyle(Color.scInkMuted)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .overlay(
                        RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                            .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                    )
                Text(source.recording)
                    .font(SCTypography.sans(size: 11))
                    .foregroundStyle(Color.scInkSecondary)
                    .lineLimit(1)
                    .frame(width: 150, alignment: .leading)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Source: \(streamLabel), \(source.recording). Opens the moment.")
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder
    private var thumbnail: some View {
        if let summary {
            // Reuse the shipped card thumbnail (recording-level poster).
            RecordingCardThumbnail(
                recording: summary,
                frameIndex: frameIndex,
                thumbnailLoader: thumbnailLoader,
                aspectRatio: 16.0 / 9.0,
                borderColor: Color.scBorderWarm
            )
        } else {
            // The recording isn't in the index — resolve the moment pointer itself
            // so the source still previews rather than a permanent blank.
            momentThumbnail
        }
    }

    private var momentThumbnail: some View {
        ZStack {
            if let pointerImage {
                Image(decorative: pointerImage.cgImage, scale: 1)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
            } else {
                LibraryHatchPlaceholder()
            }
        }
        .aspectRatio(16.0 / 9.0, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusSm))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusSm)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .task(id: source.id) { await loadPointerThumbnail() }
    }

    private func loadPointerThumbnail() async {
        guard let url = await frameIndex.resolve(recording: source.recording, anchorMs: source.timestampMs) else {
            return
        }
        let image = await thumbnailLoader.thumbnail(for: url)
        if Task.isCancelled { return }
        pointerImage = image
    }

    private var streamLabel: String {
        switch source.stream {
        case .content: return "on screen"
        case .transcript: return "audio"
        case .timeline: return "activity"
        }
    }
}
