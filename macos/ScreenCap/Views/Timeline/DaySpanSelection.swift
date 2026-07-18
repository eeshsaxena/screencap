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

/// U7 (KTD-7, R9/R19) — the "Select range" mode state machine layered over the
/// existing two-endpoint `DaySpanSelection`. It generalizes SCR-214's
/// mark-a-task selection into ONE reusable, explicit, accessible range gesture
/// that fronts Clip · Share · Delete (the SCR-214 "save as task" flow rides on
/// top: a range on footage can still be confirmed into `tasks.create`).
/// Drag-select stays REJECTED (see this file's header) — endpoints still come
/// from discrete marks (the playhead) plus arrow-key nudge for precision. Pure,
/// so every transition unit-tests without a render (DaySpanSelectionTests).
///
/// State machine (the plan's U7 range-gesture diagram):
///
///   idle → selecting → chosen → menu
///
/// with `cancel()` (Cancel / Esc) collapsing ANY state back to idle. A selection
/// MAY span "nothing captured" gaps and is NEVER discarded silently (R19) — the
/// old midpoint-in-gap silent reset is gone; the caller shows why instead
/// (delete trims to footage; clip/share error honestly).
struct RangeSelectionModel: Equatable {
    enum Phase: Equatable {
        /// Not in select-range mode.
        case idle
        /// In mode; placing / adjusting endpoints, range not yet complete.
        case selecting
        /// Both endpoints set + distinct; the action menu is anchored and the
        /// endpoints stay adjustable (nudge / re-mark).
        case chosen
        /// The action menu is presented (Clip · Share · Delete).
        case menu
    }

    private(set) var phase: Phase = .idle
    private(set) var selection = DaySpanSelection()

    /// The arrow-key nudge step — one `DaySpanSnap` snap tolerance (60 s), a
    /// boundary-scale step that reads as "one meaningful move" on a 13 h axis
    /// where 1 px ≈ minutes (req 2).
    static let nudgeStepMs = 60_000

    /// Whether the mode is active at all — drives the strip's in-flight render
    /// and the always-visible Cancel/Esc affordance.
    var isActive: Bool { phase != .idle }

    /// The ordered `[start, end]` once both endpoints are set and distinct.
    var range: (startMs: Int, endMs: Int)? { selection.range }

    /// A lone placed endpoint awaiting its partner (the strip's standalone tick).
    var pendingEndpointMs: Int? { selection.range == nil ? selection.firstMs : nil }

    /// The action menu is anchored whenever the range is complete (chosen or
    /// menu) — req 4's "once both endpoints are set, an action menu anchors".
    var menuAnchored: Bool { range != nil && (phase == .chosen || phase == .menu) }

    // MARK: - Transitions

    /// Enter select-range mode from idle (the explicit "Select range" toggle).
    /// A fresh entry always starts from an empty selection.
    mutating func enterSelectMode() {
        guard phase == .idle else { return }
        selection.reset()
        phase = .selecting
    }

    /// Cancel / Esc — leave the mode from ANY state, clearing the selection.
    /// This is the explicit, user-driven discard R19 allows (never a silent one).
    mutating func cancel() {
        selection.reset()
        phase = .idle
    }

    /// Place the next endpoint at `ms` (already boundary-snapped by the caller).
    /// Valid only while active. The selection MAY span a gap — there is NO silent
    /// reset when an endpoint or the span's midpoint lands in a "nothing
    /// captured" stretch (R19). The second distinct endpoint completes the range
    /// (`.chosen`); a further mark restarts a fresh selection
    /// (`DaySpanSelection.mark`'s third-mark rule → `.selecting`).
    mutating func mark(_ ms: Int) {
        guard isActive else { return }
        selection.mark(ms)
        phase = selection.range != nil ? .chosen : .selecting
    }

    /// Present the anchored action menu (chosen → menu). No-op unless a complete
    /// range exists.
    mutating func presentMenu() {
        guard selection.range != nil else { return }
        phase = .menu
    }

    /// Nudge the ordered start endpoint by `deltaMs` (arrow-key precision, req 2).
    mutating func nudgeStart(byMs deltaMs: Int) { nudge(orderedStart: true, deltaMs: deltaMs) }

    /// Nudge the ordered end endpoint by `deltaMs`.
    mutating func nudgeEnd(byMs deltaMs: Int) { nudge(orderedStart: false, deltaMs: deltaMs) }

    /// Move whichever raw endpoint is the ordered start/end. No-op unless both
    /// endpoints are set. Preserves the current phase (the anchored menu stays
    /// put while the user fine-tunes); a nudge that collapses the two endpoints
    /// drops the range but keeps both marks (never a silent full reset, R19).
    private mutating func nudge(orderedStart: Bool, deltaMs: Int) {
        guard isActive, let a = selection.firstMs, let b = selection.secondMs else { return }
        let firstIsStart = a <= b
        if orderedStart == firstIsStart {
            selection.firstMs = a + deltaMs
        } else {
            selection.secondMs = b + deltaMs
        }
        phase = selection.range != nil ? (phase == .menu ? .menu : .chosen) : .selecting
    }

    /// Programmatically preselect a range and open the action menu directly
    /// (req 6): "delete this day" / "delete this task" reuse the SAME flow. A
    /// zero-length request is ignored (no menu — nothing to act on).
    mutating func preselect(startMs: Int, endMs: Int) {
        guard startMs != endMs else { return }
        selection.firstMs = min(startMs, endMs)
        selection.secondMs = max(startMs, endMs)
        phase = .menu
    }
}
