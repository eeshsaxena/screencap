import Foundation

/// U2 — the pure geometry the dedicated task view feeds the reused `DayStripView`
/// and `DayPlaybackEngine`, factored out so the task-scoped axis and the
/// task-overlapping chunk selection are unit-tested without a render or disk
/// (KTD-2, KTD-3). Nothing here calls `/v0/timeline.day`; the axis and strip
/// inputs are derived from the task span alone, so opening a task never loads the
/// whole day.
enum TaskSpanLayout {
    /// Padding on each side of the task span so its band isn't flush to the strip
    /// edges (the axis reads as "this task", with a little context).
    static let padMs = 30_000

    /// KTD-3 — the task-scoped axis: the task span with a little padding on each
    /// side. For a live/in-progress task the caller passes `endMs = now` so the
    /// growing tail stays visible. Guarantees a positive span even for a
    /// sub-second task.
    static func bounds(startMs: Int, endMs: Int) -> DayStripLayout.Bounds {
        let lo = startMs - padMs
        let hi = max(endMs, startMs + 1) + padMs
        return DayStripLayout.Bounds(startMs: lo, endMs: hi)
    }

    /// KTD-2 — the chunks whose media overlaps the task span, so the player loads
    /// only the task's own footage instead of the whole day. Half-open overlap
    /// (`chunk.startMs < endMs && chunk.endMs > startMs`), matching the day-seek
    /// half-open windows.
    static func overlappingChunks(
        _ chunks: [DayPlayableChunk], startMs: Int, endMs: Int
    ) -> [DayPlayableChunk] {
        chunks.filter { $0.startMs < endMs && $0.endMs > startMs }
    }
}
