import Foundation

// SCR-214 U11 — the live "start a task" entry point (manual creation, path a).
// When ambient capture is running, the app's record affordance marks a task
// SPAN in the continuous stream rather than opening a second recording. A live
// task opens an in-progress span (rendered as a provisional card) and closes on
// an explicit stop, the next task-start, or a calendar-day roll — at which point
// it persists through `tasks.create` over `[startedAt, closedAt]`.
//
// The draft + the close rules are pure so they unit-test without a running
// daemon; `LiveTaskController` is the thin `@MainActor` glue that persists a
// closed draft through the shared `DayTasks` write-through layer.

/// An in-progress user task span not yet persisted. `recording` is the ambient
/// recording (today's continuous stream) the span belongs to; `startedAt` is its
/// wall-clock open time. The end is resolved at close time — never before start.
struct LiveTaskDraft: Identifiable, Equatable {
    let id: UUID
    var name: String
    let recording: String
    let startedAt: Date

    init(id: UUID = UUID(), name: String, recording: String, startedAt: Date) {
        self.id = id
        self.name = name
        self.recording = recording
        self.startedAt = startedAt
    }

    /// The span start in Unix seconds (the tasks store's native units).
    var startTs: Double { startedAt.timeIntervalSince1970 }

    /// The span end for a close happening at `now`, clamped so a zero/negative
    /// span can never be persisted (a same-instant stop yields a 1s minimum).
    func endTs(closingAt now: Date) -> Double {
        max(now.timeIntervalSince1970, startTs + 1)
    }

    /// Elapsed seconds since the span opened (for the provisional card's timer).
    func elapsed(at now: Date) -> TimeInterval {
        max(0, now.timeIntervalSince(startedAt))
    }
}

/// Why an open live task closed — surfaced for copy / diagnostics.
enum LiveTaskCloseReason: Equatable {
    /// The user pressed stop.
    case stopped
    /// A new live task was started, closing the previous one.
    case nextTaskStarted
    /// The local calendar day rolled while the task was open.
    case dayRolled
}

/// Pure close-timing rules, isolated so they test without the controller.
enum LiveTaskClose {
    /// The effective close instant. A day-roll closes the span at the END of the
    /// draft's local start-day (so it stays within one day, mirroring the
    /// backend's per-day recording roll — KTD1); every other reason closes at
    /// `now`.
    static func closeInstant(
        draft: LiveTaskDraft,
        reason: LiveTaskCloseReason,
        now: Date,
        calendar: Calendar = .current
    ) -> Date {
        switch reason {
        case .stopped, .nextTaskStarted:
            return now
        case .dayRolled:
            let startDay = calendar.startOfDay(for: draft.startedAt)
            // End-of-day = next midnight minus 1s; fall back to `now` if the
            // calendar can't advance (never in practice).
            guard let nextMidnight = calendar.date(byAdding: .day, value: 1, to: startDay) else {
                return now
            }
            return nextMidnight.addingTimeInterval(-1)
        }
    }

    /// Whether an open draft should day-roll-close: `now` is on a later local day
    /// than the draft opened on.
    static func hasDayRolled(draft: LiveTaskDraft, now: Date, calendar: Calendar = .current) -> Bool {
        calendar.startOfDay(for: now) > calendar.startOfDay(for: draft.startedAt)
    }
}

/// `@MainActor` glue driving the live-task lifecycle over `DayTasks`.
@MainActor
final class LiveTaskController: ObservableObject {
    /// The currently-open span, or nil. Drives the provisional card.
    @Published private(set) var draft: LiveTaskDraft?

    private let tasks: DayTasks

    init(tasks: DayTasks) {
        self.tasks = tasks
    }

    /// Open a new in-progress span against `recording`. Any already-open draft is
    /// closed first (the "next task-start closes the previous" rule) and persisted
    /// before the new one opens.
    func start(name: String, recording: String, at now: Date = Date()) async {
        if draft != nil {
            await close(reason: .nextTaskStarted, at: now)
        }
        draft = LiveTaskDraft(name: name, recording: recording, startedAt: now)
    }

    /// Live-rename the open draft (before it persists).
    func rename(_ name: String) {
        draft?.name = name
    }

    /// Explicitly stop and persist the open span. Returns true on a successful
    /// `tasks.create` (false when nothing was open or the write failed — the
    /// failure surfaces on `DayTasks.writeError`).
    @discardableResult
    func stop(at now: Date = Date()) async -> Bool {
        await close(reason: .stopped, at: now)
    }

    /// Close the open span if the calendar day has rolled since it opened,
    /// persisting it at the day boundary. No-op when nothing is open or the day
    /// hasn't rolled. Call on a periodic tick / wake.
    func closeIfDayRolled(now: Date = Date(), calendar: Calendar = .current) async {
        guard let open = draft, LiveTaskClose.hasDayRolled(draft: open, now: now, calendar: calendar) else { return }
        await close(reason: .dayRolled, at: now, calendar: calendar)
    }

    /// Persist and clear the open draft. The draft is cleared BEFORE the write so
    /// the provisional card disappears immediately; a write failure is surfaced by
    /// `DayTasks` (with a retryable create) rather than re-opening the draft.
    @discardableResult
    private func close(reason: LiveTaskCloseReason, at now: Date, calendar: Calendar = .current) async -> Bool {
        guard let open = draft else { return false }
        draft = nil
        let closeAt = LiveTaskClose.closeInstant(draft: open, reason: reason, now: now, calendar: calendar)
        return await tasks.create(
            recording: open.recording,
            name: open.name,
            startTs: open.startTs,
            endTs: open.endTs(closingAt: closeAt)
        )
    }
}
