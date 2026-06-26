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

    enum Phase: Equatable {
        case idle
        case searching
        case loaded(SearchResults)
        case daemonDown
    }

    /// Per-stream row cap applied to every daemon query. `timeline.query`
    /// truncates earliest-first, so this also bounds the snap pool the
    /// transcript correlation draws from.
    private static let searchResultLimit = 200

    private let service: SearchService
    private let parser: QueryParser
    private let now: @Sendable () -> Date
    /// Recording chunk length (seconds), read once from `settings` by the live
    /// wiring (set on the view model after the settings fetch). Used to estimate
    /// a transcript chunk's wall-clock position before snapping it to a real
    /// timeline event.
    var chunkDurationSeconds: Double
    /// Monotonic search token for latest-wins publishing (#2). Bumped at each
    /// `search(_:)` entry; a run only publishes its result if it is still the
    /// current generation when it finishes.
    private var generation = 0

    init(
        service: SearchService = LiveSearchService(),
        parser: QueryParser = QueryParser(),
        // Matches `config.get_chunk_duration()`'s 900 s (15 min) default; the
        // live wiring overwrites this from `settings --json` when present.
        chunkDurationSeconds: Double = 900,
        now: @escaping @Sendable () -> Date = Date.init
    ) {
        self.service = service
        self.parser = parser
        self.chunkDurationSeconds = chunkDurationSeconds
        self.now = now
    }

    /// `contentIndexEnabled` is read by the caller via `settings --json` — the
    /// consent trigger keys on the flag, not on `index_state` (which only
    /// signals an absent index file).
    func search(_ query: String, contentIndexEnabled: Bool) async {
        let parsed = parser.parse(query, now: now())
        guard !parsed.isEmpty else {
            phase = .idle
            return
        }

        // Latest-wins: capture a monotonic token at entry and only publish if
        // still current. An overlapping newer search supersedes this one rather
        // than being dropped (#2) — the SwiftUI callsite also cancels the prior
        // Task, so `Task.isCancelled` short-circuits a superseded run early.
        generation &+= 1
        let token = generation
        phase = .searching

        let hasFreeText = !parsed.freeText.isEmpty

        async let timelineFetch = fetchTimeline(parsed)
        async let contentFetch: Fetched<ContentPayload> =
            hasFreeText ? fetchContent(parsed.freeText) : .notRun
        async let transcriptFetch: Fetched<[TranscriptHit]> =
            hasFreeText ? fetchTranscript(parsed.freeText) : .notRun

        let timeline = await timelineFetch
        let content = await contentFetch
        let transcript = await transcriptFetch

        guard !Task.isCancelled, token == generation else { return }

        // All three share one socket: a socket-level failure on the
        // always-attempted timeline call means the daemon is unreachable.
        if case .down = timeline {
            phase = .daemonDown
            return
        }

        var items: [SearchResultItem] = []
        var idx = 0
        var capReached = false
        func nextID(_ stream: SearchResultItem.Stream, _ recording: String, _ anchor: Int?) -> String {
            defer { idx += 1 }
            return "\(stream.rawValue)-\(recording)-\(anchor.map(String.init) ?? "u")-\(idx)"
        }

        // Timeline (authoritative, already server-filtered).
        var timelineState: StreamState = .notRun
        if case .ok(let rows) = timeline {
            timelineState = rows.isEmpty ? .empty : .ok(count: rows.count)
            if rows.count >= Self.searchResultLimit { capReached = true }
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
        // Note: a `.down` timeline already returned `.daemonDown` above, so it
        // never reaches here.

        // Content / on-screen text (best-effort). Client-side time filter.
        var contentState: StreamState = .notRun
        if case .ok(let payload) = content {
            let filtered = payload.hits.filter { inWindow($0.timestampMs, parsed.timeWindow) }
            contentState = mapContentState(payload.indexState, matched: !filtered.isEmpty, count: filtered.count)
            if payload.hits.count >= Self.searchResultLimit { capReached = true }
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
        } else if case .down = content {
            contentState = .unavailable
        }

        // Transcript / audio (best-effort). Correlate chunk_index -> a real
        // captured moment via per-recording timeline.query, then time-filter.
        var transcriptState: StreamState = .notRun
        if case .ok(let hits) = transcript {
            if hits.count >= Self.searchResultLimit { capReached = true }
            let anchored = await correlateTranscript(hits)
            guard !Task.isCancelled, token == generation else { return }
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
        } else if case .down = transcript {
            transcriptState = .unavailable
        }

        let results = SearchResults(
            items: rank(items),
            coverage: CoverageReport(screen: contentState, audio: transcriptState, activity: timelineState),
            consentNeeded: hasFreeText && !contentIndexEnabled,
            timeWindow: parsed.timeWindow,
            appFilter: parsed.appFilter,
            capReached: capReached
        )
        phase = .loaded(results)
    }

    // MARK: - Fetch helpers (each maps socket-level failure to `.down`)

    private enum Fetched<Payload> {
        case notRun
        case down
        case errored
        case ok(Payload)
    }

    private struct ContentPayload { let hits: [ContentHit]; let indexState: ContentIndexState }

    private func fetchTimeline(_ p: ParsedQuery) async -> Fetched<[TimelineRow]> {
        do {
            let resp = try await service.timelineQuery(TimelineQueryRequest(
                startMs: p.timeWindow?.startMs, endMs: p.timeWindow?.endMs,
                app: p.appFilter, limit: Self.searchResultLimit
            ))
            return .ok(resp.rows)
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
        } catch {
            return .errored
        }
    }

    private func fetchContent(_ freeText: String) async -> Fetched<ContentPayload> {
        do {
            let resp = try await service.contentSearch(ContentSearchRequest(query: freeText, limit: Self.searchResultLimit))
            return .ok(ContentPayload(hits: resp.hits, indexState: resp.indexState))
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
        } catch {
            return .errored
        }
    }

    private func fetchTranscript(_ freeText: String) async -> Fetched<[TranscriptHit]> {
        do {
            let resp = try await service.transcriptSearch(TranscriptSearchRequest(query: freeText, limit: Self.searchResultLimit))
            return .ok(resp.hits)
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
        } catch {
            return .errored
        }
    }

    // MARK: - Transcript correlation

    private struct AnchoredTranscript { let hit: TranscriptHit; let anchorMs: Int? }

    /// For each recording with transcript hits, place each chunk at
    /// `recordingStart + chunkIndex * chunkDuration` snapped to the nearest real
    /// timeline event (a captured moment). A recording with no timeline rows
    /// yields an unanchored hit (surfaced off the timeline).
    ///
    /// The per-recording timeline queries run CONCURRENTLY via a task group
    /// (#3 — was a serial N+1), each bounded to a window that surrounds the
    /// chunk estimates (#6 — `timeline.query` truncates earliest-first, so an
    /// unbounded `limit` query would only ever return the OLDEST events and miss
    /// the snap pool for any late chunk).
    private func correlateTranscript(_ hits: [TranscriptHit]) async -> [AnchoredTranscript] {
        guard !Task.isCancelled else { return [] }
        let byRecording = Dictionary(grouping: hits, by: { $0.recording })

        let anchoredByRecording: [String: [AnchoredTranscript]] = await withTaskGroup(
            of: (String, [AnchoredTranscript]).self
        ) { group in
            for (recording, recHits) in byRecording {
                group.addTask { [self] in
                    guard !Task.isCancelled else {
                        return (recording, recHits.map { AnchoredTranscript(hit: $0, anchorMs: nil) })
                    }
                    return (recording, await self.anchorRecording(recording, hits: recHits))
                }
            }
            var collected: [String: [AnchoredTranscript]] = [:]
            for await (recording, anchored) in group {
                collected[recording] = anchored
            }
            return collected
        }

        // Flatten in a stable recording order so output is deterministic.
        return byRecording.keys.sorted().flatMap { anchoredByRecording[$0] ?? [] }
    }

    /// Snap one recording's transcript hits to its real timeline events. First
    /// resolves the recording start (earliest events — the verb truncates
    /// earliest-first, so `.first` is reliable), then snaps within a window
    /// bounded to surround the chunk estimates.
    private func anchorRecording(_ recording: String, hits: [TranscriptHit]) async -> [AnchoredTranscript] {
        guard let start = await recordingStartMs(recording) else {
            return hits.map { AnchoredTranscript(hit: $0, anchorMs: nil) }
        }

        let chunkMs = Int((chunkDurationSeconds * 1000).rounded())
        let estimates = hits.map { start + $0.chunkIndex * chunkMs }
        guard let minEst = estimates.min(), let maxEst = estimates.max() else {
            return hits.map { AnchoredTranscript(hit: $0, anchorMs: nil) }
        }

        // Bound the snap pool to ±chunkDuration around the estimate span so the
        // nearest captured moment surrounds each estimate (#6).
        let pool = await recordingTimestamps(
            recording, startMs: minEst - chunkMs, endMs: maxEst + chunkMs
        )
        let snapPool = pool.isEmpty ? [start] : pool

        return zip(hits, estimates).map { hit, estimate in
            AnchoredTranscript(hit: hit, anchorMs: nearest(estimate, in: snapPool))
        }
    }

    /// The recording's start time (ms) = its earliest captured event. Uses the
    /// verb's earliest-first truncation: a small unbounded query reliably yields
    /// the first event.
    private func recordingStartMs(_ recording: String) async -> Int? {
        do {
            let resp = try await service.timelineQuery(TimelineQueryRequest(recording: recording, limit: Self.searchResultLimit))
            return resp.rows.map { $0.timestampMs }.min()
        } catch {
            return nil
        }
    }

    private func recordingTimestamps(_ recording: String, startMs: Int, endMs: Int) async -> [Int] {
        do {
            let resp = try await service.timelineQuery(TimelineQueryRequest(
                startMs: startMs, endMs: endMs, recording: recording, limit: Self.searchResultLimit
            ))
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

    /// Recency-first (most recent anchor leads); unanchored items sort last.
    /// `score` (bm25, more-negative = better) breaks ties among same-instant hits.
    private func rank(_ items: [SearchResultItem]) -> [SearchResultItem] {
        items.sorted { a, b in
            switch (a.anchorMs, b.anchorMs) {
            case let (x?, y?):
                if x != y { return x > y }
                return a.score < b.score
            case (_?, nil): return true
            case (nil, _?): return false
            case (nil, nil): return a.score < b.score
            }
        }
    }

    private func mapContentState(_ state: ContentIndexState, matched: Bool, count: Int) -> StreamState {
        switch state {
        case .ok: return matched ? .ok(count: count) : .empty
        case .noMatch: return .empty
        case .notIndexed: return .notIndexed
        case .indexDegraded: return matched ? .ok(count: count) : .degraded
        case .storeUnavailable: return .unavailable
        }
    }
}
