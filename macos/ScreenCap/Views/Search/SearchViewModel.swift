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
    }

    /// SCR-182 U3 — per-stream daemon fetch cap. The wire carries no `has_more`,
    /// so a raw stream returning exactly this many rows is treated as truncated
    /// upstream (more matched than were returned).
    static let streamFetchLimit = 200

    private let service: SearchService
    private let parser: QueryParser
    private let now: @Sendable () -> Date
    /// Recording chunk length (seconds), read once from `settings` by the live
    /// wiring (set on the view model after the settings fetch). Used to estimate
    /// a transcript chunk's wall-clock position before snapping it to a real
    /// timeline event.
    var chunkDurationSeconds: Double

    init(
        service: SearchService = LiveSearchService(),
        parser: QueryParser = QueryParser(),
        chunkDurationSeconds: Double = 300,
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

        isSearching = true
        defer { isSearching = false }
        // Keep prior results visible during an in-place refresh; only fall back to
        // the full-screen spinner for the first search from a non-loaded state.
        if case .loaded = phase {} else { phase = .searching }

        let hasFreeText = !parsed.freeText.isEmpty

        async let timelineFetch = fetchTimeline(parsed)
        async let contentFetch: Fetched<ContentPayload> =
            hasFreeText ? fetchContent(parsed.freeText) : .notRun
        async let transcriptFetch: Fetched<[TranscriptHit]> =
            hasFreeText ? fetchTranscript(parsed.freeText) : .notRun

        let timeline = await timelineFetch
        let content = await contentFetch
        let transcript = await transcriptFetch

        // A superseded live-search must not publish over a newer one's result —
        // bail at every publish point once this task has been cancelled.
        if Task.isCancelled { return }

        // All three share one socket: a socket-level failure on the
        // always-attempted timeline call means the daemon is unreachable.
        if case .down = timeline {
            phase = .daemonDown
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
            let filtered = payload.hits.filter { inWindow($0.timestampMs, parsed.timeWindow) }
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
        // Re-check after the transcript-correlation awaits: a newer search may
        // have superseded this one while the fan-out was in flight.
        if Task.isCancelled { return }
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
                app: p.appFilter, limit: Self.streamFetchLimit
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
            let resp = try await service.contentSearch(ContentSearchRequest(query: freeText, limit: Self.streamFetchLimit))
            return .ok(ContentPayload(hits: resp.hits, indexState: resp.indexState))
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            return .down
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
            // Stop issuing per-recording timeline.query calls once superseded —
            // under live typing this fan-out is the dominant socket load.
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
