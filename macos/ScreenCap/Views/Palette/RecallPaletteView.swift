import AppKit
import SwiftUI

// U10 — the Recall palette (design 554–597): a command-palette overlay over the
// main window (KTD-4) re-presenting the existing search stack — SearchViewModel,
// SearchService, SnippetHighlighter, ranking, recents — unchanged (KTD-2).
// Dimmed scrim, 620pt panel at top-center, keyboard-first: ↑↓ browse (wrapping),
// ↵ jumps to the Day timeline at the hit moment, esc dismisses and cancels the
// in-flight query. Consent + backfill states embed inside the panel.

/// The stateful overlay: owns the model, query lifecycle, selection, and
/// dismissal; renders `RecallPaletteContent` (pure, hostable in tests).
struct RecallPaletteView: View {
    @Binding var isPresented: Bool
    /// ↵ / click on a hit — MainWindow routes to the Day timeline (U9).
    var onJump: (Date, Int) -> Void

    @StateObject private var model = SearchViewModel()
    @StateObject private var recentStore = RecentSearchesStore()
    @State private var runner: RecallPaletteQueryRunner?
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()

    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var consentDeclined = false
    @State private var backfillDeclined = false
    @State private var selectedResultID: SearchResultItem.ID?
    @FocusState private var fieldFocused: Bool

    var body: some View {
        ZStack(alignment: .top) {
            scrim
            panel
                .frame(width: 620)
                .padding(.top, 60)
        }
        .task {
            runner = RecallPaletteQueryRunner { [weak model] text in
                await model?.search(text, contentIndexEnabled: contentIndexEnabled)
            }
            await loadSettings()
            fieldFocused = true
        }
        .onChange(of: query) { _ in runner?.search(query, debounced: true) }
        .onChange(of: model.phase) { phase in
            if case .loaded = phase { selectedResultID = nil }
        }
    }

    private var scrim: some View {
        Color.scDarkCanvas.opacity(0.35)
            .ignoresSafeArea()
            .contentShape(Rectangle())
            .onTapGesture { dismiss() }
            .accessibilityHidden(true)
    }

    private var panel: some View {
        VStack(spacing: 0) {
            header
            RecallPaletteContent(
                phase: model.phase,
                consentDeclined: consentDeclined,
                backfillState: model.backfillState,
                recentSearches: recentStore.recent,
                queryTerms: queryTerms,
                selectedResultID: selectedResultID,
                frameIndex: frameIndex,
                thumbnailLoader: thumbnailLoader,
                onEnableConsent: enableConsent,
                onDeclineConsent: declineConsent,
                onAcceptBackfill: model.acceptBackfill,
                onSkipBackfill: skipBackfill,
                onCancelBackfill: model.cancelBackfill,
                onResumeBackfill: model.resumeBackfill,
                onRunChip: runChipQuery,
                onOpen: jump
            )
            footer
        }
        .background(Color.scSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .shadow(color: Color.scDarkCanvas.opacity(0.35), radius: 30, y: 24)
        .onExitCommand { dismiss() }
        .background(keyboardNav)
    }

    // MARK: - Header (design 557–562)

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(Color.scTeal)
                .accessibilityHidden(true)
            TextField("Search any moment", text: $query)
                .textFieldStyle(.plain)
                .font(SCTypography.sans(size: 16))
                .foregroundStyle(Color.scInk)
                .focused($fieldFocused)
                .onSubmit { submit() }
                .accessibilityLabel("Search any moment. Searches only what's on this Mac.")
            Spacer(minLength: 8)
            Text("searching this Mac only")
                .font(SCTypography.mono(size: 11))
                .foregroundStyle(Color.scInkMuted)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 16)
        .overlay(alignment: .bottom) { Divider().overlay(Color.scFillSubtle) }
    }

    // MARK: - Footer (design 591–594)

    private var footer: some View {
        HStack {
            Text("↑↓ browse · ↵ jump to moment · esc close")
                .font(SCTypography.mono(size: 10.5))
                .foregroundStyle(Color.scInkMuted)
            Spacer()
            Text("indexed on-device")
                .font(SCTypography.mono(size: 10.5))
                .foregroundStyle(Color.scTeal)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(Color.scCanvas)
        .overlay(alignment: .top) { Divider().overlay(Color.scFillSubtle) }
    }

    /// ↑↓ via invisible shortcut buttons — they fire while the text field holds
    /// focus, moving the selection with wrap (RecallPalette.next/previous).
    private var keyboardNav: some View {
        Group {
            Button("") { selectedResultID = RecallPalette.next(after: effectiveID, in: orderedItems) }
                .keyboardShortcut(.downArrow, modifiers: [])
            Button("") { selectedResultID = RecallPalette.previous(before: effectiveID, in: orderedItems) }
                .keyboardShortcut(.upArrow, modifiers: [])
        }
        .opacity(0)
        .accessibilityHidden(true)
    }

    // MARK: - Derived

    private var orderedItems: [SearchResultItem] {
        guard case .loaded(let results) = model.phase else { return [] }
        return RecallPalette.orderedItems(results)
    }

    private var effectiveID: SearchResultItem.ID? {
        RecallPalette.effectiveSelection(selectedResultID, in: orderedItems)?.id
    }

    private var queryTerms: [String] {
        guard case .loaded(let results) = model.phase else { return [] }
        return results.queryTerms
    }

    // MARK: - Actions

    /// ↵: jump to the selected (or first) hit; with no results yet, commit the
    /// query immediately (bypassing the live debounce).
    private func submit() {
        if let item = RecallPalette.effectiveSelection(selectedResultID, in: orderedItems) {
            jump(item)
            return
        }
        recentStore.record(query)
        runner?.search(query, debounced: false)
    }

    private func jump(_ item: SearchResultItem) {
        guard let target = RecallPalette.timelineTarget(for: item) else { return }
        recentStore.record(query)
        dismiss()
        onJump(target.day, target.seekMs)
    }

    private func runChipQuery(_ text: String) {
        query = text
        recentStore.record(text)
        runner?.search(text, debounced: false)
    }

    /// esc / scrim-click: cancel the in-flight query, then close.
    private func dismiss() {
        runner?.cancel()
        isPresented = false
    }

    // MARK: - Settings + consent (inherited from the retired SearchView, palette-scoped)

    private func loadSettings() async {
        do {
            let data = try await CLIClient.runJSONRaw(["settings", "--json"])
            let env = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            contentIndexEnabled = env.settings.contentIndexEnabled ?? false
            consentDeclined = env.settings.contentIndexConsentDeclined ?? false
            backfillDeclined = env.settings.contentIndexBackfillDeclined ?? false
            if let dur = env.settings.chunkDuration { model.chunkDurationSeconds = dur }
        } catch {
            contentIndexEnabled = false
        }
    }

    /// Enable on-screen-text indexing going forward, then re-run the query —
    /// optimistic with revert-on-failure (the retired SearchView's
    /// enableConsent pattern).
    private func enableConsent() {
        contentIndexEnabled = true
        model.offerBackfill(alreadyDeclined: backfillDeclined)
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_enabled=true", "--json"]
                )
            } catch {
                contentIndexEnabled = false
                model.dismissBackfillOffer()
                return
            }
            runner?.search(query, debounced: false)
        }
    }

    private func declineConsent() {
        consentDeclined = true
        Task {
            do {
                _ = try await CLIClient.runJSONRaw(
                    ["settings", "--set", "content_index_consent_declined=true", "--json"]
                )
            } catch {
                consentDeclined = false
            }
        }
    }

    private func skipBackfill() {
        backfillDeclined = true
        model.skipBackfill()
    }
}

/// The palette's pure body: renders one `RecallPalette.State` variant plus the
/// grouped result rows. No live model, no sockets — hostable by
/// RecallPaletteStateTests with SearchFixtures.
struct RecallPaletteContent: View {
    let phase: SearchViewModel.Phase
    let consentDeclined: Bool
    let backfillState: SearchViewModel.BackfillUIState
    let recentSearches: [String]
    let queryTerms: [String]
    let selectedResultID: SearchResultItem.ID?
    let frameIndex: RecordingFrameIndex?
    let thumbnailLoader: ThumbnailLoader?
    var onEnableConsent: () -> Void = {}
    var onDeclineConsent: () -> Void = {}
    var onAcceptBackfill: () -> Void = {}
    var onSkipBackfill: () -> Void = {}
    var onCancelBackfill: () -> Void = {}
    var onResumeBackfill: () -> Void = {}
    var onRunChip: (String) -> Void = { _ in }
    var onOpen: (SearchResultItem) -> Void = { _ in }

    private var state: RecallPalette.State {
        RecallPalette.state(
            phase: phase,
            consentDeclined: consentDeclined,
            backfillState: backfillState,
            recents: recentSearches
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if state.showsConsentBanner { consentBanner }
            if state.backfill != .hidden { backfillSection }
            bodyContent
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var bodyContent: some View {
        switch state.body {
        case .idle(let recents):
            idleState(recents)
        case .searching:
            HStack {
                Spacer()
                ProgressView().controlSize(.small)
                Spacer()
            }
            .padding(.vertical, 28)
        case .daemonDown:
            message(
                icon: "bolt.horizontal.circle",
                title: "ScreenCap isn't running",
                note: "Start ScreenCap's background helper to search your history."
            )
        case .empty:
            message(
                icon: "magnifyingglass",
                title: "No matches on this Mac",
                note: "Try different words, an app name, or a time like \u{201C}yesterday afternoon\u{201D}."
            )
        case .results:
            resultsList
        }
    }

    // MARK: - Results (design 564–590)

    @ViewBuilder
    private var resultsList: some View {
        if case .loaded(let results) = phase {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(RecallPalette.groups(results), id: \.label) { group in
                        Text(group.label)
                            .font(SCTypography.mono(size: 10))
                            .tracking(1.0)
                            .foregroundStyle(Color.scInkMuted)
                            .accessibilityAddTraits(.isHeader)
                        ForEach(group.items, id: \.id) { item in
                            RecallPaletteRow(
                                item: item,
                                queryTerms: queryTerms,
                                isSelected: item.id == effectiveSelectedID(results),
                                frameIndex: frameIndex,
                                thumbnailLoader: thumbnailLoader,
                                onOpen: { onOpen(item) }
                            )
                        }
                    }
                }
                .padding(14)
            }
            .frame(maxHeight: 420)
        }
    }

    private func effectiveSelectedID(_ results: SearchResults) -> SearchResultItem.ID? {
        RecallPalette.effectiveSelection(selectedResultID, in: RecallPalette.orderedItems(results))?.id
    }

    // MARK: - Idle (recents, U10 test scenario)

    private func idleState(_ recents: [String]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Search everything you've recorded — words on screen, spoken audio, apps.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
            if !recents.isEmpty {
                Text("RECENT")
                    .font(SCTypography.mono(size: 10))
                    .tracking(1.0)
                    .foregroundStyle(Color.scInkMuted)
                FlowChips(texts: recents, onTap: onRunChip)
            }
        }
        .padding(20)
    }

    // MARK: - Consent banner (copy carried over from the retired Search pane)

    private var consentBanner: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Search on-screen text too?")
                .font(SCTypography.sans(size: 13, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Text("Turn this on to also search the text that was on your screen. Newly recorded screens become searchable — it all stays on this Mac and is never uploaded.")
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Not now", action: onDeclineConsent)
                Button("Turn on", action: onEnableConsent)
                    .buttonStyle(.borderedProminent)
                    .tint(Color.scTeal)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.scTealSoft.opacity(0.4))
        .overlay(alignment: .bottom) { Divider().overlay(Color.scFillSubtle) }
    }

    // MARK: - Backfill affordance (SCR-178 states, palette-compact)

    @ViewBuilder
    private var backfillSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch state.backfill {
            case .hidden:
                EmptyView()
            case .offering:
                Text("Index your existing recordings now?")
                    .font(SCTypography.sans(size: 13, weight: .semibold))
                Text("Searches only cover what's been indexed. This runs on this Mac and can be paused anytime.")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkSecondary)
                HStack(spacing: 8) {
                    Button("Skip", action: onSkipBackfill)
                    Button("Index now", action: onAcceptBackfill)
                        .buttonStyle(.borderedProminent)
                        .tint(Color.scTeal)
                }
            case .starting:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Preparing to index…")
                        .font(SCTypography.sans(size: 12))
                    Spacer()
                    Button("Cancel", action: onCancelBackfill)
                }
            case .indexing(let done, let total, _):
                HStack(spacing: 8) {
                    ProgressView(value: Double(done), total: Double(max(total, 1)))
                        .frame(width: 140)
                    Text("Indexed \(done) of \(total)")
                        .font(SCTypography.sans(size: 12))
                    Spacer()
                    Button("Cancel", action: onCancelBackfill)
                }
            case .done(let done, let total, let failed):
                Text(failed == 0 ? "All set — \(done) indexed." : "Indexing finished — \(done) of \(total), \(failed) failed.")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkSecondary)
            case .paused(let done, let total), .cancelled(let done, let total):
                HStack(spacing: 8) {
                    Text("Indexed \(done) of \(total) so far — resume to continue.")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkSecondary)
                    Spacer()
                    Button("Resume", action: onResumeBackfill)
                }
            case .startFailed:
                Text("Couldn't start indexing — try again later.")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkSecondary)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.scCanvas)
        .overlay(alignment: .bottom) { Divider().overlay(Color.scFillSubtle) }
    }

    // MARK: - Message state

    private func message(icon: String, title: String, note: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 22))
                .foregroundStyle(Color.scInkFaint)
            Text(title)
                .font(SCTypography.sans(size: 13, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Text(note)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 28)
        .padding(.horizontal, 20)
    }
}

/// One palette result row (design 566–590): 128pt 16/9 thumbnail with a time
/// chip, bold title, quoted snippet with highlighted terms, and stream + time
/// chips. The selected row carries the teal tint + border.
struct RecallPaletteRow: View {
    let item: SearchResultItem
    let queryTerms: [String]
    let isSelected: Bool
    let frameIndex: RecordingFrameIndex?
    let thumbnailLoader: ThumbnailLoader?
    var onOpen: () -> Void

    @State private var loaded: Loaded?

    private struct Loaded: Equatable {
        let key: String
        let image: ThumbnailImage?
        static func == (lhs: Loaded, rhs: Loaded) -> Bool {
            lhs.key == rhs.key && (lhs.image?.cgImage === rhs.image?.cgImage)
        }
    }

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .top, spacing: 12) {
                thumbnail
                info
                Spacer(minLength: 0)
            }
            .padding(10)
            .background(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .fill(isSelected ? Color.scTeal.opacity(0.07) : Color.clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .strokeBorder(isSelected ? Color.scTeal.opacity(0.27) : Color.scFillSubtle, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(SearchAccessibility.resultRowLabel(item))
        .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : .isButton)
        .task(id: thumbKey) { await loadThumbnail() }
    }

    private var thumbnail: some View {
        ZStack {
            if loaded?.key == thumbKey, let image = loaded?.image {
                Image(decorative: image.cgImage, scale: 1)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
            } else {
                LibraryHatchPlaceholder()
            }
        }
        .frame(width: 128, height: 72)
        .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusSm))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusSm)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .overlay(alignment: .bottomTrailing) {
            if item.anchorMs != nil {
                Text(item.timeLabel)
                    .font(SCTypography.mono(size: 9))
                    .foregroundStyle(Color.scCanvas)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.scInk.opacity(0.85), in: RoundedRectangle(cornerRadius: 3))
                    .padding(5)
            }
        }
        .accessibilityHidden(true)
    }

    private var info: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(titleText)
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .lineLimit(1)
            if let snippet = snippetText {
                Text(snippet)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkSecondary)
                    .lineLimit(2)
            }
            HStack(spacing: 6) {
                Text(streamChipText)
                    .font(SCTypography.mono(size: 9.5))
                    .foregroundStyle(Color.scInkMuted)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 2)
                    .overlay(
                        RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                            .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                    )
                Text(timeChipText)
                    .font(SCTypography.mono(size: 9.5))
                    .foregroundStyle(Color.scInkMuted)
            }
            .padding(.top, 3)
        }
    }

    /// Bold title: window title / app / recording — the design's sample
    /// meeting-title lead line, from what the daemon actually returns.
    private var titleText: String {
        item.title ?? item.app ?? item.recording
    }

    /// Quoted snippet with the matched terms bolded (screen/audio only —
    /// activity rows have no searchable snippet).
    private var snippetText: AttributedString? {
        guard item.stream != .activity, let raw = item.snippet, !raw.isEmpty else { return nil }
        return SnippetHighlighter.attributed("\u{201C}\(raw)\u{201D}", terms: queryTerms)
    }

    /// The design's task-label chip claims task names that arrive with
    /// SCR-214 — until then the chip states the hit's stream honestly.
    private var streamChipText: String {
        switch item.stream {
        case .screen: return "on screen"
        case .audio: return item.approximate ? "audio · time approximate" : "audio"
        case .activity: return "activity"
        }
    }

    private var timeChipText: String {
        item.anchorMs == nil ? "time unknown" : "\(item.timeLabel) · moment"
    }

    private var thumbKey: String {
        "\(item.recording)#\(item.anchorMs.map(String.init) ?? "-")"
    }

    private func loadThumbnail() async {
        guard let frameIndex, let thumbnailLoader else { return }
        let key = thumbKey
        guard let url = await frameIndex.resolve(recording: item.recording, anchorMs: item.anchorMs) else {
            if Task.isCancelled { return }
            loaded = Loaded(key: key, image: nil)
            return
        }
        let image = await thumbnailLoader.thumbnail(for: url)
        if Task.isCancelled { return }
        loaded = Loaded(key: key, image: image)
    }
}

/// Recent-search chips, wrapping horizontally (idle state).
private struct FlowChips: View {
    let texts: [String]
    var onTap: (String) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(texts, id: \.self) { text in
                Button {
                    onTap(text)
                } label: {
                    Text(text)
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkSecondary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(Color.scCanvas, in: Capsule())
                        .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
                        .lineLimit(1)
                }
                .buttonStyle(.plain)
            }
        }
    }
}
