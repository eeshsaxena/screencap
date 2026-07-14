import Foundation

// SCR-214 U11 — the retroactive span-select entry point (manual creation,
// path b, AE3). The user marks two endpoints on the Day-timeline; each snaps to
// the nearest task / recording boundary so a new task abuts its neighbours
// cleanly, then a label sheet confirms it into `tasks.create`.
//
// This is the two-endpoint variant of the plan's "drag-to-select": both
// endpoints come from the playhead (seek → "Mark start", seek → "Mark end"),
// which reuses the strip's existing seek gesture rather than adding a fragile
// drag mode to the shared `DayStripView`. Kept pure so the selection + snapping
// rules unit-test without a render (DaySpanSelectionTests).

/// A two-endpoint retroactive task selection. Endpoints are absolute unix ms.
struct DaySpanSelection: Equatable {
    /// The first marked endpoint (nil until the user marks a start).
    var firstMs: Int?
    /// The second marked endpoint (nil until the user marks an end).
    var secondMs: Int?

    /// Both endpoints marked.
    var isComplete: Bool { firstMs != nil && secondMs != nil }

    /// The ordered `[start, end]` once both endpoints are set and distinct; nil
    /// while incomplete or when the two endpoints coincide (a zero-length span is
    /// rejected client-side before the verb, matching the daemon's 400).
    var range: (startMs: Int, endMs: Int)? {
        guard let a = firstMs, let b = secondMs, a != b else { return nil }
        return (min(a, b), max(a, b))
    }

    /// Mark the next endpoint. First mark sets the start; second sets the end; a
    /// third restarts the selection from a fresh start (so re-marking is cheap).
    mutating func mark(_ ms: Int) {
        if firstMs == nil {
            firstMs = ms
        } else if secondMs == nil {
            secondMs = ms
        } else {
            firstMs = ms
            secondMs = nil
        }
    }

    mutating func reset() {
        firstMs = nil
        secondMs = nil
    }
}

/// Snapping + recording-resolution for a retroactive selection. Pure.
enum DaySpanSnap {
    /// All snap boundaries for a day: each recording base track's start/end and
    /// each task band's start/end, deduped + sorted. Snapping to these keeps a
    /// new user task flush against existing tasks / the recording edges.
    static func boundaries(baseTracks: [DayStripBaseTrack], segments: [DayStripSegment]) -> [Int] {
        var set = Set<Int>()
        for base in baseTracks {
            set.insert(base.startMs)
            set.insert(base.endMs)
        }
        for segment in segments {
            set.insert(segment.startMs)
            set.insert(segment.endMs)
        }
        return set.sorted()
    }

    /// Snap `ms` to the nearest boundary within `toleranceMs`; otherwise leave it
    /// unchanged. A 60 s default reads as "close enough to a boundary to mean it".
    static func snap(_ ms: Int, to boundaries: [Int], toleranceMs: Int = 60_000) -> Int {
        guard let nearest = boundaries.min(by: { abs($0 - ms) < abs($1 - ms) }) else { return ms }
        return abs(nearest - ms) <= toleranceMs ? nearest : ms
    }

    /// The recording whose base track contains the selection's midpoint — the
    /// recording the new task is created against. Nil when the midpoint falls in a
    /// "nothing captured" gap between recordings (no footage to attach a task to).
    static func recording(forRangeMidpoint startMs: Int, _ endMs: Int, baseTracks: [DayStripBaseTrack]) -> String? {
        let mid = startMs + (endMs - startMs) / 2
        return baseTracks.first { $0.startMs <= mid && mid <= $0.endMs }?.recording
    }
}
