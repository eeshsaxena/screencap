import Foundation

// SCR-180 U1 — pure, side-effect-free blended relevance + recency ranking for
// in-app search results. Replaces the old recency-only comparator in
// `SearchViewModel.rank`: a free-text query now blends a per-stream relevance
// signal with normalized recency so a relevant text/transcript hit reliably
// outranks an unrelated-but-newer activity row (origin R4), while recency still
// orders items *within* a relevance tier. With no free text every item is an
// activity row (relevance 0) and the blend collapses to pure recency.
//
// Kept in its own file (internal, not file-private) so the scoring is directly
// unit-testable and the weights live in one tunable place — tuning the blend
// against real recordings means editing `BlendWeights.default`, nothing else.

/// The tunable knobs of the blend. Defaults are the shipped starting point;
/// final values are tuned against real recordings (SCR-180 deferred work). The
/// **dominance invariant** below is the load-bearing property — it, not the
/// exact numbers, is what guarantees the R4 acceptance:
///
///     relevanceWeight * textFloor  >  recencyWeight * 1.0
///
/// i.e. any text hit (relevance ≥ `textFloor`) outranks any activity row
/// (relevance 0) regardless of recency. With the defaults:
/// `0.75 * 0.5 = 0.375 > 0.25 * 1.0 = 0.25` ✓. Keep this invariant true when
/// retuning, or text hits stop reliably beating newer activity.
struct BlendWeights: Equatable, Sendable {
    /// Weight on the relevance axis (text match strength).
    var relevanceWeight: Double
    /// Weight on the recency axis (how recently the moment was captured).
    var recencyWeight: Double
    /// Floor of the text-relevance band: the *weakest* content hit and every
    /// transcript hit map to at least this, so text always clears activity (0)
    /// by the dominance invariant. Content hits scale across `[textFloor, 1.0]`.
    var textFloor: Double
    /// Fixed relevance for a transcript (audio) hit. Transcript matches carry no
    /// graded bm25 score on the wire, so they sit at a constant inside the text
    /// band — a match, but not graded against content hits.
    var transcriptRelevance: Double

    static let `default` = BlendWeights(
        relevanceWeight: 0.75,
        recencyWeight: 0.25,
        textFloor: 0.5,
        transcriptRelevance: 0.6
    )
}

enum SearchRanking {
    /// Order `items` by the blended relevance + recency score, descending, with
    /// a deterministic total order so equal-blend items sort stably (required
    /// for testable ordering — Swift's `sort` is not guaranteed stable).
    static func rankBlended(_ items: [SearchResultItem], weights: BlendWeights = .default) -> [SearchResultItem] {
        guard items.count > 1 else { return items }

        // Set-level normalization bounds. bm25 is computed over content/screen
        // hits only (the only stream carrying a graded score); recency over every
        // anchored item across all streams.
        let bm25Bounds = minMax(items.filter { $0.stream == .screen }.map(\.score))
        let anchorBounds = minMax(items.compactMap(\.anchorMs).map(Double.init))

        let scored = items.map { item -> (item: SearchResultItem, blended: Double) in
            let relevance = relevance(of: item, bm25Bounds: bm25Bounds, weights: weights)
            let recency = recency(of: item, anchorBounds: anchorBounds)
            return (item, weights.relevanceWeight * relevance + weights.recencyWeight * recency)
        }

        return scored.sorted { a, b in
            if a.blended != b.blended { return a.blended > b.blended }
            // Deterministic tiebreakers: newer first (nils last), then stronger
            // bm25 (more negative), then id for full stability.
            switch (a.item.anchorMs, b.item.anchorMs) {
            case let (x?, y?) where x != y: return x > y
            case (_?, nil): return true
            case (nil, _?): return false
            default: break
            }
            if a.item.score != b.item.score { return a.item.score < b.item.score }
            return a.item.id < b.item.id
        }.map(\.item)
    }

    // MARK: - Per-stream relevance

    /// Relevance is assigned by `stream`, never inferred from `score` alone:
    /// `score == 0` is overloaded between an activity row (no relevance) and a
    /// transcript hit (a text match with no graded score), so keying on the
    /// stream is the only correct discriminator.
    private static func relevance(
        of item: SearchResultItem, bm25Bounds: (min: Double, max: Double)?, weights: BlendWeights
    ) -> Double {
        switch item.stream {
        case .screen:
            // bm25: more-negative = better. Map best (min) → 1.0, worst (max) →
            // 0, then scale into [textFloor, 1.0]. A single hit / all-equal set
            // (min == max) maps to the band top so a lone strong match keeps
            // full relevance rather than collapsing to the floor.
            guard let bounds = bm25Bounds, bounds.max != bounds.min else {
                return 1.0
            }
            let norm = (item.score - bounds.max) / (bounds.min - bounds.max)
            return weights.textFloor + norm * (1.0 - weights.textFloor)
        case .audio:
            return weights.transcriptRelevance
        case .activity:
            return 0.0
        }
    }

    // MARK: - Recency

    /// Newest anchored moment → 1.0, oldest → 0.0. Unanchored items (transcript
    /// hits whose chunk couldn't be resolved) get 0.0 — the oldest position on
    /// the recency axis — but keep their text relevance, so a relevant unanchored
    /// hit still outranks a zero-relevance activity row (consistent with R3),
    /// just below anchored text hits. An all-same-anchor set maps to 1.0 (recency
    /// can't differentiate; the tiebreakers decide).
    private static func recency(of item: SearchResultItem, anchorBounds: (min: Double, max: Double)?) -> Double {
        guard let anchor = item.anchorMs else { return 0.0 }
        guard let bounds = anchorBounds, bounds.max != bounds.min else { return 1.0 }
        return (Double(anchor) - bounds.min) / (bounds.max - bounds.min)
    }

    private static func minMax(_ values: [Double]) -> (min: Double, max: Double)? {
        guard let first = values.first else { return nil }
        var lo = first, hi = first
        for v in values.dropFirst() {
            if v < lo { lo = v }
            if v > hi { hi = v }
        }
        return (lo, hi)
    }
}
