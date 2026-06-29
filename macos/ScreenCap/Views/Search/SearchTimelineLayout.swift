import CoreGraphics
import Foundation

// SCR-181 U1 — the pure geometry + clustering core for the scrubbable per-day
// search timeline. SwiftUI-free and file-scope so it is unit-testable without a
// render (mirrors `TimelinePaneScrub` / `SearchRanking`). The view (U2) is a thin
// Canvas/overlay over these functions; the clustering threshold and day bounds
// are parameters so the view tunes them in one place.

/// A placed node on the day axis: one or more results sharing an x position.
/// `items.count == 1` is a single marker; `> 1` is a density cluster. The
/// representative x is the mean of the members' x positions.
struct TimelineNode: Equatable {
    let items: [SearchResultItem]
    let x: CGFloat

    var count: Int { items.count }
    var isCluster: Bool { items.count > 1 }
    /// The item a single-marker tap opens, or the earliest member of a cluster.
    var representative: SearchResultItem { items[0] }
}

enum SearchTimelineLayout {

    /// A result placed at an x position on the axis, before clustering.
    struct Marker: Equatable {
        let item: SearchResultItem
        let x: CGFloat
    }

    /// Map each anchored item's `anchorMs` to an x in `[0, width]` within the day
    /// bounds `[startMs, endMs]`. Unanchored items are skipped (they have no time
    /// and never appear on the axis). Out-of-bounds times clamp to the edges. A
    /// non-positive `width` or a non-positive span places every marker at x=0 — no
    /// NaN, no divide-by-zero (mirrors `TimelinePaneScrub`).
    static func placeMarkers(
        _ items: [SearchResultItem], startMs: Int, endMs: Int, width: CGFloat
    ) -> [Marker] {
        let span = Double(endMs - startMs)
        guard width > 0, span > 0 else {
            return items.compactMap { $0.anchorMs == nil ? nil : Marker(item: $0, x: 0) }
        }
        return items.compactMap { item in
            guard let ms = item.anchorMs else { return nil }
            let ratio = min(1, max(0, Double(ms - startMs) / span))
            return Marker(item: item, x: CGFloat(ratio) * width)
        }
    }

    /// Collapse markers into nodes by single-linkage chaining over x: walking
    /// left-to-right, a marker joins the current cluster when it is within
    /// `thresholdPx` of the previous marker, otherwise it starts a new one. This
    /// honors the intuitive contract "markers within `thresholdPx` cluster"
    /// (a fixed-bin scheme splits two near markers that straddle a bin edge), and
    /// a uniformly dense day still collapses to a small node count (adjacent gaps
    /// all ≤ threshold chain together), so a busy day stays legible (R5a). Gaps
    /// wider than the threshold keep distinct clumps apart, preserving the time
    /// distribution. A non-positive threshold disables clustering. Cluster x is
    /// the mean of member xs; members are ordered by x then id, nodes by x.
    static func cluster(_ markers: [Marker], thresholdPx: CGFloat) -> [TimelineNode] {
        let sorted = markers.sorted { ($0.x, $0.item.id) < ($1.x, $1.item.id) }
        guard thresholdPx > 0, !sorted.isEmpty else {
            return sorted.map { TimelineNode(items: [$0.item], x: $0.x) }
        }
        func node(_ bucket: [Marker]) -> TimelineNode {
            let meanX = bucket.reduce(CGFloat(0)) { $0 + $1.x } / CGFloat(bucket.count)
            return TimelineNode(items: bucket.map(\.item), x: meanX)
        }
        var nodes: [TimelineNode] = []
        var bucket: [Marker] = [sorted[0]]
        for marker in sorted.dropFirst() {
            if marker.x - bucket[bucket.count - 1].x <= thresholdPx {
                bucket.append(marker)
            } else {
                nodes.append(node(bucket))
                bucket = [marker]
            }
        }
        nodes.append(node(bucket))
        return nodes
    }

    /// The node nearest a tapped/scrubbed x. `nil` when there are no nodes. Ties
    /// resolve to the first (lower-x) node since `min(by:)` is stable on equal
    /// distances.
    static func nodeAt(_ x: CGFloat, in nodes: [TimelineNode]) -> TimelineNode? {
        nodes.min { abs($0.x - x) < abs($1.x - x) }
    }

    /// The `[startMs, endMs)` day window containing `anchorMs`: local midnight to
    /// +24h. A fixed 24h span (not "next local midnight") keeps the axis uniform
    /// across days and avoids a ±1h DST anomaly skewing marker placement.
    static func dayBounds(forAnchorMs ms: Int, calendar: Calendar = .current) -> (startMs: Int, endMs: Int) {
        let date = Date(timeIntervalSince1970: Double(ms) / 1000)
        let startMs = Int(calendar.startOfDay(for: date).timeIntervalSince1970 * 1000)
        return (startMs, startMs + 86_400_000)
    }
}
