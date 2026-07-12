import Combine
import Foundation

// SCR-174 U4 — orchestrates an in-app search. Parses the query locally, fans
// out to the three daemon verbs as INDEPENDENT calls (one stream failing must
// not fail the others), client-side time-filters the streams the daemon can't,
// correlates transcript hits to a real captured moment via timeline.query,
// merges + ranks by recency, and reports honest per-stream coverage. Pure logic
// behind a `SearchService` seam so it is unit-testable without a live socket.

@MainActor
final class SearchViewModel: ObservableObject {
    @Published private(set) var phase: Phase = .idle
    /// SCR-182 U2 — true while a search runs. When a refresh starts over an
    /// already-`.loaded` phase, prior results stay visible and this drives a
    /// lightweight inline indicator instead of the full-screen spinner. Only
    /// meaningful while `phase == .loaded` (ignored in other phases).
    @Published private(set) var isSearching = false

    enum Phase: Equatable {
        case idle
        case searching
        case loaded(SearchResults)
        case daemonDown
        /// Paid-only launch (U12): the daemon returned 402 `subscription_required`
        /// from the recall verbs — the user is lapsed / not-entitled and the local
        /// paywall is enforced. DISTINCT from `.daemonDown` (helper unreachable)
        /// and per-stream `.unavailable` (a store error) so the surface reads as
        /// "upgrade to search", not "search is broken". The five recall verbs all
        /// gate together, so the always-attempted timeline fetch surfacing this is
        /// authoritative for the whole search.
        case subscriptionRequired
    }

    /// SCR-182 U3 — per-stream daemon fetch cap. The wire carries no `has_more`,
    /// so a raw stream returning exactly this many rows is treated as truncated
    /// upstream (more matched than were returned).
    static let streamFetchLimit = 200

    // MARK: - SCR-178 U8 — backfill affordance state machine

    /// The "also index existing recordings" affordance, modeled as its own state
    /// container so it is **independent of `consentNeeded`**. This is load-bearing:
    /// the moment the user taps "Turn on", `content_index_enabled` flips, so the
    /// next search reports `consentNeeded == false` and the consent banner
    /// disappears. If the backfill progress UI were gated on `consentNeeded` it
    /// would vanish the instant indexing was enabled — exactly when the user
    /// needs to watch it. Driving the UI off this enum (never `consentNeeded`)
    /// keeps the affordance on screen across that flip.
    enum BackfillUIState: Equatable {
        /// Nothing shown (no offer pending, or the user skipped, or it finished
        /// and was dismissed).
        case hidden
        /// "Index your existing recordings now?" with Accept / Skip.
        case offering
        /// Accepted; `backfillStart` issued, but no progress event yet — the
        /// engine seeds its closed set first so `total` is briefly 0.
        /// Indeterminate ("Preparing to index…").
        case starting
        /// Determinate progress once `total > 0`.
        case indexing(done: Int, total: Int, failed: Int)
        /// Terminal: every unit processed. `failed == 0` → clean copy;
        /// `failed > 0` → hedged copy.
        case done(done: Int, total: Int, failed: Int)
        /// Terminal: budget/large-library pause — resumable. Carries a Resume.
        case paused(done: Int, total: Int)
        /// Terminal: user cancelled — resumable. Carries a Resume.
        case cancelled(done: Int, total: Int)
        /// Terminal: `backfillStart` was rejected / daemon unreachable. Search
        /// stays fully usable; the user can retry later.
        case startFailed

        /// Whether the affordance currently owns the keyboard (Return stands
        /// down): true while an offer is up or a run is in flight, so the hidden
        /// open-selected-result Return handler doesn't steal the key.
        var isActive: Bool {
            switch self {
            case .offering, .starting, .indexing:
                return true
            case .hidden, .done, .paused, .cancelled, .startFailed:
                return false
            }
        }

        /// Whether this state is a terminal outcome (drives the once-only
        /// VoiceOver announcement; in-progress ticks must not announce).
        var isTerminal: Bool {
            switch self {
            case .done, .paused, .cancelled, .startFailed:
                return true
            case .hidden, .offering, .starting, .indexing:
                return false
            }
        }
    }

    @Published private(set) var backfillState: BackfillUIState = .hidden

    private let service: SearchService
    private let backfill: BackfillService
    /// Persists a `content_index_backfill_declined=true` (Skip) the same way the
    /// consent decline is persisted (CLI `settings --set`). Injected so the
    /// state machine is testable without spawning the CLI; the live wiring passes
    /// the real CLI call. Throwing surfaces as a no-op (the affordance still
    /// hides locally — see `skipBackfill`).
    private let persistBackfillDeclined: @Sendable () async throws -> Void
    /// `var` so SCR-179 U5 can rebuild it once per session with the index-sourced
    /// vocabulary unioned onto the static seed (see `refreshVocabularyIfNeeded`).
    private var parser: QueryParser
    /// One-shot guard: the vocabulary is fetched once per session before the
    /// first parse, never per keystroke.
    private var vocabularyLoaded = false
    private let now: @Sendable () -> Date
    /// Recording chunk length (seconds), read once from `settings` by the live
    /// wiring (set on the view model after the settings fetch). Used to estimate
    /// a transcript chunk's wall-clock position before snapping it to a real
    /// timeline event.
    var chunkDurationSeconds: Double
    /// The live progress-consumption task — cancelled when the affordance is
    /// dismissed or a new run starts.
    private var backfillTask: Task<Void, Never>?

    init(
        service: SearchService = LiveSearchService(),
        backfill: BackfillService = LiveBackfillService(),
        persistBackfillDeclined: @escaping @Sendable () async throws -> Void = {
            _ = try await CLIClient.runJSONRaw(
                ["settings", "--set", "content_index_backfill_declined=true", "--json"]
            )
        },
        parser: QueryParser = QueryParser(),
        chunkDurationSeconds: Double = 300,
        now: @escaping @Sendable () -> Date = Date.init
    ) {
        self.service = service
        self.backfill = backfill
        self.persistBackfillDeclined = persistBackfillDeclined
        self.parser = parser
        self.chunkDurationSeconds = chunkDurationSeconds
        self.now = now
    }

    /// `contentIndexEnabled` is read by the caller via `settings --json` — the
    /// consent trigger keys on the flag, not on `index_state` (which only
    /// signals an absent index file).
    func search(_ query: String, contentIndexEnabled: Bool) async {
        // SCR-179 U5 — fetch the index-sourced vocabulary once before the first
        // parse so site/app recognition reflects the user's real recordings.
        // Runs on @MainActor, so the guard + rebuild can't race the parse below.
        await refreshVocabularyIfNeeded()

        let parsed = parser.parse(query, now: now())
        guard !parsed.isEmpty else {
            phase = .idle
            return
        }

        isSearching = true
        defer { isSearching = false }
        // SCR-182 U2 — keep prior results visible during an in-place refresh; only
        // fall back to the full-screen spinner for the first search from a
        // non-loaded state.
        if case .loaded = phase {} else { phase = .searching }

        let hasFreeText = !parsed.freeText.isEmpty
        // SCR-176 — `timeline.query` is filtered by time/app only (it carries no
        // free-text term) and truncates earliest-first, so an unbounded fetch for a
        // pure free-text query would flood results with up to `streamFetchLimit` of
        // the OLDEST, unrelated activity rows. Only fan out to timeline when there
        // is a time window or app filter to bound it; a pure free-text query leaves
        // the timeline stream `.notRun`.
        let hasTimelineQuery = parsed.timeWindow != nil || parsed.appFilter != nil

        async let timelineFetch: Fetched<[TimelineRow]> =
            hasTimelineQuery ? fetchTimeline(parsed) : .notRun
        async let contentFetch: Fetched<ContentPayload> =
            hasFreeText ? fetchContent(parsed.freeText) : .notRun
        async let transcriptFetch: Fetched<[TranscriptHit]> =
            hasFreeText ? fetchTranscript(parsed.freeText) : .notRun

        let timeline = await timelineFetch
        let content = await contentFetch
        let transcript = await transcriptFetch

        // SCR-182 U1 — a superseded live-search must not publish over a newer
        // one's result; bail at every publish point once this task is cancelled.
        if Task.isCancelled { return }

        // All streams share one socket. A socket-level failure on ANY attempted
        // stream means the daemon is unreachable — derive daemon-down from
        // whichever streams ran, since a pure free-text query no longer attempts
        // timeline (SCR-176). `.notRun` is neither down nor gated, and the
        // `!parsed.isEmpty` guard above guarantees at least one stream ran, so this
        // stays a reliable daemon-down signal for every query shape.
        if timeline.isDown || content.isDown || transcript.isDown {
            phase = .daemonDown
            return
        }
        // U12: the local paywall gates all five recall verbs together, so a 402
        // `subscription_required` on any attempted stream is authoritative for the
        // whole search — the user is lapsed. Surface the dedicated upgrade phase,
        // distinct from `.daemonDown` and any per-stream `.unavailable`, before
        // assembling partial results.
        if timeline.isSubscriptionRequired || content.isSubscriptionRequired
            || transcript.isSubscriptionRequired {
            phase = .subscriptionRequired
            return
        }

        var items: [SearchResultItem] = []
        // SCR-182 U3 — set when any stream's RAW fetch hit the cap (before the
        // client-side time filter below). Scoped to these main fetches only — the
        // per-recording correlation queries always request the cap and must not
        // feed this.
        var truncated = false
        var idx = 0
        func nextID(_ stream: SearchResultItem.Stream, _ recording: String, _ anchor: Int?) -> String {
            defer { idx += 1 }
            return "\(stream.rawValue)-\(recording)-\(anchor.map(String.init) ?? "u")-\(idx)"
        }

        // Timeline (authoritative, already server-filtered).
        var timelineState: StreamState = .notRun
        if case .ok(let rows) = timeline {
            timelineState = rows.isEmpty ? .empty : .ok(count: rows.count)
            if rows.count >= Self.streamFetchLimit { truncated = true }
            for row in rows {
                items.append(SearchResultItem(
                    id: nextID(.activity, row.recording, row.timestampMs),
                    stream: .activity, recording: row.recording,
                    anchorMs: row.timestampMs, approximate: false, score: 0,
                    snippet: nil, app: row.app, title: row.title
                ))
            }
        } else if case .errored = timeline {
            timelineState = .unavailable
        }

        // Content / on-screen text (best-effort). Client-side time filter.
        var contentState: StreamState = .notRun
        if case .ok(let payload) = content {
            if payload.hits.count >= Self.streamFetchLimit { truncated = true }
            // Drop title-union hits (match_source == "title"): they carry the
            // sentinel timestampMs = 0, not a frame pointer, so mapping them into
            // the on-screen-text stream would render a bogus t=0 result (or get
            // dropped by the time-window filter). The daemon/CLI still surface
            // renamed recordings by title; surfacing them in this macOS Search UI
            // as a distinct title match is a follow-up (SCR-223).
            let filtered = payload.hits.filter {
                $0.matchSource != "title" && inWindow($0.timestampMs, parsed.timeWindow)
            }
            contentState = mapContentState(payload.indexState, matched: !filtered.isEmpty)
            for hit in filtered {
                items.append(SearchResultItem(
                    id: nextID(.screen, hit.recording, hit.timestampMs),
                    stream: .screen, recording: hit.recording,
                    anchorMs: hit.timestampMs, approximate: false, score: hit.score,
                    snippet: hit.snippet, app: nil, title: nil
                ))
            }
        } else if case .errored = content {
            contentState = .unavailable
        }

        // Transcript / audio (best-effort). Correlate chunk_index -> a real
        // captured moment via per-recording timeline.query, then time-filter.
        var transcriptState: StreamState = .notRun
        if case .ok(let hits) = transcript {
            if hits.count >= Self.streamFetchLimit { truncated = true }
            let anchored = await correlateTranscript(hits)
            let filtered = anchored.filter { inWindow($0.anchorMs, parsed.timeWindow) }
            transcriptState = filtered.isEmpty ? .empty : .ok(count: filtered.count)
            for a in filtered {
                items.append(SearchResultItem(
                    id: nextID(.audio, a.hit.recording, a.anchorMs),
                    stream: .audio, recording: a.hit.recording,
                    anchorMs: a.anchorMs, approximate: true, score: 0,
                    snippet: a.hit.snippet, app: nil, title: nil
                ))
            }
        } else if case .errored = transcript {
            transcriptState = .unavailable
        }

        let results = SearchResults(
            items: rank(items),
            coverage: CoverageReport(screen: contentState, audio: transcriptState, activity: timelineState),
            consentNeeded: hasFreeText && !contentIndexEnabled,
            timeWindow: parsed.timeWindow,
            appFilter: parsed.appFilter,
            queryTerms: parsed.freeText.split(whereSeparator: { $0.isWhitespace }).map(String.init),
            truncated: truncated
        )
        // SCR-182 U1 — re-check after the transcript-correlation awaits: a newer
        // search may have superseded this one while the fan-out was in flight.
        if Task.isCancelled { return }
        phase = .loaded(results)
    }

    // MARK: - SCR-179 U5 — index-sourced parser vocabulary

    /// Fetch `/v0/apps.list` once per session and rebuild the parser with the
    /// index-derived vocabulary unioned onto the static seed. Strictly fail-soft:
    /// a throw (incl. an older daemon's 404, or the daemon being down) leaves the
    /// static-vocabulary parser in place so search stays fully usable. The
    /// hostnames returned are a same-EUID-only browsing-profile artifact (R6) —
    /// they are consumed here to derive recognition terms and never persisted,
    /// re-emitted, or logged.
    private func refreshVocabularyIfNeeded() async {
        guard !vocabularyLoaded else { return }
        // Set first (on @MainActor) so a second `search` during the await does
        // not issue a duplicate fetch.
        vocabularyLoaded = true
        do {
            let vocab = try await service.appsList()
            let terms = QueryParser.vocabularyTerms(
                appNames: vocab.appNames, hostnames: vocab.hostnames
            )
            guard !terms.isEmpty else { return }
            parser = QueryParser(
                calendar: parser.calendar,
                knownApps: QueryParser.defaultKnownApps + terms
            )
        } catch {
            // Fail-soft: keep the static-vocabulary parser.
        }
    }

    // MARK: - SCR-178 U8 — backfill affordance transitions

    /// Show the "index existing recordings now?" offer. Called from
    /// `enableConsent()` right after the flag flips — unless the user already
    /// skipped the backfill before (then it stays hidden, no re-nag).
    func offerBackfill(alreadyDeclined: Bool) {
        guard !alreadyDeclined else {
            backfillState = .hidden
            return
        }
        backfillState = .offering
    }

    /// Accept the offer: subscribe to progress **before** starting the run (so
    /// the initial events aren't missed — the late-listener race), move to
    /// `starting`, then issue `backfillStart`. A start failure → `startFailed`
    /// (search stays usable). Resume (from `cancelled`/`paused`) routes here too.
    func acceptBackfill() {
        startRun()
    }

    /// Resume a `cancelled`/`paused` run — same path as accept (the daemon
    /// resumes from the ledger).
    func resumeBackfill() {
        startRun()
    }

    private func startRun() {
        backfillTask?.cancel()
        backfillState = .starting
        // Subscribe FIRST so events published right after `start()` are caught
        // (the late-listener race). `progressEvents()` is invoked here, before
        // the `start()` call below, so the listener is attached before the run.
        //
        // One task — await `start()`, then drain the progress stream inline,
        // returning on the terminal event. Deliberately NOT a nested
        // `Task { … }` + `await consume.value`: a @MainActor task awaiting
        // another @MainActor task's value wedges on resume (the prior run's
        // consume never completes on a still-open stream, so its `.value`
        // never resolves). Inlining the loop and breaking on the terminal
        // event keeps a single, self-completing task.
        let events = backfill.progressEvents()
        backfillTask = Task { [weak self] in
            guard let self else { return }
            do {
                let status = try await self.backfill.start()
                // Reflect the seed snapshot only if no progress event has moved
                // us off `starting` yet (usually total==0 → stays `starting`).
                if self.backfillState == .starting {
                    self.applyStatus(status)
                }
            } catch {
                self.setStartFailed()
                return
            }
            for await event in events {
                if Task.isCancelled { return }
                self.applyEvent(event)
                if event.isTerminal { return }
            }
        }
    }

    /// Skip the offer: persist the decline (same CLI path as consent decline) so
    /// it doesn't re-prompt, hide the affordance, and start no job. Indexing
    /// stays forward-only. A persistence failure still hides locally (best
    /// effort — the offer just may reappear on a later launch).
    func skipBackfill() {
        backfillTask?.cancel()
        backfillState = .hidden
        Task { [persistBackfillDeclined] in
            try? await persistBackfillDeclined()
        }
    }

    /// Cancel an in-flight run. Optimistic local transition to `cancelled` with
    /// a Resume; the daemon flushes its ledger so a resume continues cleanly.
    func cancelBackfill() {
        // Stop draining progress FIRST, so a `.running` event already in flight
        // can't land after we set `.cancelled` and clobber the optimistic
        // transition (mirrors the cancel-first pattern in startRun/skipBackfill).
        backfillTask?.cancel()
        let (d, t) = currentCounts()
        // Capture the service by value (it is Sendable) rather than `[weak self]`
        // so the daemon cancel verb isn't silently dropped if the view model is
        // released before the await completes.
        Task { [backfill] in
            _ = try? await backfill.cancel()
        }
        backfillState = .cancelled(done: d, total: t)
    }

    /// Retract a pending offer without persisting a decline — used when the
    /// consent write itself failed, so the affordance shouldn't linger. No job
    /// was started, nothing to cancel.
    func dismissBackfillOffer() {
        if case .offering = backfillState { backfillState = .hidden }
    }

    private func setStartFailed() {
        backfillState = .startFailed
    }

    /// Map a streamed progress event onto the affordance state. Terminal events
    /// land on the matching terminal state; `backfill.progress` ticks update the
    /// determinate `indexing` (or stay `starting` until `total > 0`).
    private func applyEvent(_ event: BackfillProgressEvent) {
        applyStatus(BackfillStatus(
            state: event.state,
            done: event.done,
            skipped: event.skipped,
            failed: event.failed,
            total: event.total,
            currentUnitIndex: event.currentUnitIndex
        ))
    }

    /// Pure-ish mapper from a daemon `BackfillStatus` snapshot to `BackfillUIState`.
    /// Used by both the seed snapshot and each progress event. `skipped` is a
    /// privacy-correct outcome (frames the policy blocked) — it is never surfaced
    /// as an error; only `failed > 0` hedges the copy.
    private func applyStatus(_ status: BackfillStatus) {
        switch status.state {
        case .running:
            backfillState = status.total > 0
                ? .indexing(done: status.done, total: status.total, failed: status.failed)
                : .starting
        case .completed:
            backfillState = .done(done: status.done, total: status.total, failed: status.failed)
        case .paused:
            backfillState = .paused(done: status.done, total: status.total)
        case .cancelled:
            backfillState = .cancelled(done: status.done, total: status.total)
        case .failed:
            // A run that fails outright after starting reads like a start
            // failure to the user: indexing didn't complete, search is still
            // usable, retry later.
            backfillState = .startFailed
        case .idle:
            // No run in flight — leave the current affordance untouched (a stale
            // idle snapshot must not wipe an offer or a terminal result).
            break
        }
    }

    /// Best-known (done, total) for the current state — used to seed the Resume
    /// affordance on cancel so it shows the progress reached.
    private func currentCounts() -> (Int, Int) {
        switch backfillState {
        case .indexing(let d, let t, _): return (d, t)
        case .done(let d, let t, _): return (d, t)
        case .paused(let d, let t): return (d, t)
        case .cancelled(let d, let t): return (d, t)
        case .hidden, .offering, .starting, .startFailed: return (0, 0)
        }
    }

    // MARK: - Fetch helpers (each maps socket-level failure to `.down`)

    private enum Fetched<Payload> {
        case notRun
        case down
        /// Daemon 402 `subscription_required` (U12) — the local paywall gated this
        /// recall verb. Told apart from `.errored` so the model can raise the
        /// dedicated `.subscriptionRequired` phase (upgrade CTA), not the generic
        /// per-stream `.unavailable`.
        case subscriptionRequired
        case errored
        case ok(Payload)

        /// A socket-level transport failure (daemon unreachable). Checked across
        /// every attempted stream (SCR-176) so daemon-down is still detected when a
        /// pure free-text query skips the timeline verb.
        var isDown: Bool {
            if case .down = self { return true }
            return false
        }

        /// The local-paywall 402 gate. Any attempted stream carrying it is
        /// authoritative — the five recall verbs gate together.
        var isSubscriptionRequired: Bool {
            if case .subscriptionRequired = self { return true }
            return false
        }
    }

    /// The daemon envelope code for the local-paywall gate (mirrors
    /// `errors.py::SUBSCRIPTION_REQUIRED`). Surfaced as a 402 `envelopeError`.
    private static let subscriptionRequiredCode = "subscription_required"

    private struct ContentPayload { let hits: [ContentHit]; let indexState: ContentIndexState }

    private func fetchTimeline(_ p: ParsedQuery) async -> Fetched<[TimelineRow]> {
        do {
            let resp = try await service.timelineQuery(TimelineQueryRequest(
                startMs: p.timeWindow?.startMs, endMs: p.timeWindow?.endMs,
                app: p.appFilter, limit: Self.streamFetchLimit
            ))
            return .ok(resp.rows)
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
        } catch DaemonClientError.envelopeError(Self.subscriptionRequiredCode, _) {
            return .subscriptionRequired
        } catch {
            return .errored
        }
    }

    private func fetchContent(_ freeText: String) async -> Fetched<ContentPayload> {
        do {
            let resp = try await service.contentSearch(ContentSearchRequest(query: freeText, limit: Self.streamFetchLimit))
            return .ok(ContentPayload(hits: resp.hits, indexState: resp.indexState))
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
        } catch DaemonClientError.envelopeError(Self.subscriptionRequiredCode, _) {
            return .subscriptionRequired
        } catch {
            return .errored
        }
    }

    private func fetchTranscript(_ freeText: String) async -> Fetched<[TranscriptHit]> {
        do {
            let resp = try await service.transcriptSearch(TranscriptSearchRequest(query: freeText, limit: Self.streamFetchLimit))
            return .ok(resp.hits)
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
        } catch DaemonClientError.envelopeError(Self.subscriptionRequiredCode, _) {
            return .subscriptionRequired
        } catch {
            return .errored
        }
    }

    // MARK: - Transcript correlation

    private struct AnchoredTranscript { let hit: TranscriptHit; let anchorMs: Int? }

    /// For each recording with transcript hits, query its timeline once, then
    /// place each chunk at `recordingStart + chunkIndex * chunkDuration` snapped
    /// to the nearest real timeline event (a captured moment). A recording with
    /// no timeline rows yields an unanchored hit (surfaced off the timeline).
    private func correlateTranscript(_ hits: [TranscriptHit]) async -> [AnchoredTranscript] {
        let byRecording = Dictionary(grouping: hits, by: { $0.recording })
        var out: [AnchoredTranscript] = []
        for (recording, recHits) in byRecording {
            // SCR-182 U1 — stop issuing per-recording timeline.query calls once
            // superseded; under live typing this fan-out is the dominant socket load.
            if Task.isCancelled { break }
            let timestamps = await recordingTimestamps(recording)
            guard let start = timestamps.first else {
                out.append(contentsOf: recHits.map { AnchoredTranscript(hit: $0, anchorMs: nil) })
                continue
            }
            for hit in recHits {
                let estimate = start + Int((Double(hit.chunkIndex) * chunkDurationSeconds * 1000).rounded())
                let snapped = nearest(estimate, in: timestamps)
                out.append(AnchoredTranscript(hit: hit, anchorMs: snapped))
            }
        }
        return out
    }

    private func recordingTimestamps(_ recording: String) async -> [Int] {
        do {
            let resp = try await service.timelineQuery(TimelineQueryRequest(recording: recording, limit: 200))
            return resp.rows.map { $0.timestampMs }.sorted()
        } catch {
            return []
        }
    }

    private func nearest(_ target: Int, in sorted: [Int]) -> Int? {
        guard !sorted.isEmpty else { return nil }
        return sorted.min(by: { abs($0 - target) < abs($1 - target) })
    }

    // MARK: - Filtering, ranking, coverage

    private func inWindow(_ ms: Int?, _ window: TimeWindow?) -> Bool {
        guard let window else { return true }
        guard let ms else { return false }  // unanchored can't satisfy a window
        return ms >= window.startMs && ms < window.endMs
    }

    /// SCR-180 — blended relevance + recency ordering. A free-text query weights
    /// per-stream relevance (content bm25 / transcript text match) against
    /// normalized recency so a relevant text hit outranks an unrelated, newer
    /// activity row (origin R4), while recency still orders within a relevance
    /// tier. With no free text every item is an activity row (relevance 0) and
    /// the blend collapses to pure recency. The scoring + the weight-dominance
    /// invariant live in `SearchRanking` so the blend is tunable in one place.
    private func rank(_ items: [SearchResultItem]) -> [SearchResultItem] {
        SearchRanking.rankBlended(items)
    }

    private func mapContentState(_ state: ContentIndexState, matched: Bool) -> StreamState {
        switch state {
        case .ok: return matched ? .ok(count: 1) : .empty
        case .noMatch: return .empty
        case .notIndexed: return .notIndexed
        case .indexDegraded: return matched ? .ok(count: 1) : .degraded
        case .storeUnavailable: return .unavailable
        }
    }
}

// MARK: - View-facing result types

struct SearchResults: Equatable, Sendable {
    var items: [SearchResultItem]
    var coverage: CoverageReport
    /// Free-text present but on-screen-text indexing is off — drives the U7
    /// consent CTA (the flag is the trigger, not `index_state`).
    var consentNeeded: Bool
    /// The resolved interpretation, surfaced to the user ("searched yesterday
    /// afternoon").
    var timeWindow: TimeWindow?
    var appFilter: String?
    /// The parsed free-text terms (SCR-177 U3) — used by `ResultRow` to highlight
    /// matched terms in content/transcript snippets. Defaulted so existing
    /// constructions (tests, older call sites) need not supply it.
    var queryTerms: [String] = []
    /// SCR-182 U3 — true when at least one stream's raw fetch hit `streamFetchLimit`
    /// (more matched than were returned). Drives the "narrow your search" cue.
    /// Defaulted so existing constructions need not supply it.
    var truncated: Bool = false
}

struct SearchResultItem: Identifiable, Equatable, Sendable {
    enum Stream: String, Sendable { case screen, audio, activity }

    let id: String
    let stream: Stream
    let recording: String
    /// Placement on the per-day timeline; `nil` = unanchored (surfaced off the
    /// timeline — currently only transcript hits whose chunk can't be resolved).
    let anchorMs: Int?
    let approximate: Bool
    let score: Double
    let snippet: String?
    let app: String?
    let title: String?
}

/// Honest per-stream coverage. `screen` (content) carries the index-state
/// distinctions; `audio`/`activity` only distinguish ran/empty/unavailable.
struct CoverageReport: Equatable, Sendable {
    var screen: StreamState
    var audio: StreamState
    var activity: StreamState
}

enum StreamState: Equatable, Sendable {
    case notRun        // free-text empty → stream not queried
    case ok(count: Int)
    case empty         // searched, no matches
    case notIndexed    // on-screen text indexing off / no index file (content only)
    case degraded      // FTS5 absent → LIKE fallback (content only)
    case unavailable   // store corrupt or the call errored
}
