import SwiftUI

// U9 (day diary) — the morning resume card: on app open after a work gap, a
// dismissible IN-APP nudge on the Days landing surface names the most recent
// thread and where the user left off, jumping back to that block (F3, R11, R12,
// AE4). It is a landing nudge, NOT global nav chrome (KTD-11), so it lives on the
// Days view — never the persistent shell sidebar.
//
// The gating logic is a PURE predicate (mirrors `ShellSidebarModel
// .shouldShowLocalModelHint`) so R11/R12/AE4 are directly unit-testable without a
// render or a live daemon. Card content is computed FRESH from `tasks.query` on
// each appearance — never cached (KTD-11): a since-deleted day yields no rows, so
// the predicate returns nil by construction (no card can outlive its evidence).

/// The resolved content of a resume card — everything the view renders plus the
/// deep-link pointer (KTD-2: keyed by `block_id`, never `task_index`) and the
/// persisted dismissal key. Equatable so the pure predicate is directly assertable.
struct ResumeCardContent: Equatable {
    /// The local start-of-day of the most recent recorded day (the jump target).
    let day: Date
    /// The human day label ("Friday, 17 July") for the card title (AE4).
    let dayLabel: String
    /// The thread's name = the stopping block's name (never a recording/window, R2).
    let threadName: String
    /// The stopping block's stable identity — the deep-link key (KTD-2). Nil only
    /// on an older daemon that omits it; the jump still lands via the span.
    let blockId: String?
    /// The stopping block's absolute-ms span — the seek anchor + landing highlight.
    let startMs: Int
    let endMs: Int
    /// "HH:mm" at the block's end — "left off at 17:24" (F3).
    let stoppingClock: String
    /// The persisted dismissal key ("day|thread"); a new day yields a new key, so a
    /// later day re-arms the card even after an earlier one was dismissed (R11).
    let dismissalKey: String

    /// The landing highlight span for the day page (AE3 pattern) — the block extent.
    var highlight: DaySpanHighlight { DaySpanHighlight(startMs: startMs, endMs: endMs) }
}

/// Pure model behind the resume card — the gap threshold and the gating predicate,
/// factored out of the view so R11/R12/AE4 are assertable without a live daemon
/// (the `ShellSidebarModel` pattern).
enum ResumeCardModel {

    /// The work-gap threshold: the card fires only when the gap since the last
    /// recorded activity exceeds this (default 4h). `get_rest_threshold`'s 120s
    /// grain is deliberately NOT reused — it measures within-session rests, the
    /// wrong grain for "you stepped away from work" (KTD-11). A Swift-side constant
    /// is right: the card is computed entirely client-side from the fresh
    /// `tasks.query`, so there is no server-computed gap to mirror.
    static let resumeGapThreshold: TimeInterval = 4 * 3600

    /// Build the dismissal key for a (day, thread) pair. The thread identity is the
    /// stable `thread_id`, falling back to the `block_id`, then the name — so a lone
    /// (unthreaded) block still keys stably and a new day always yields a new key.
    static func dismissalKey(day: String, threadIdentity: String) -> String {
        "\(day)|\(threadIdentity)"
    }

    /// Decide whether the morning resume card should show, and with what content —
    /// PURE (no I/O), so the caller passes the fresh `tasks.query` days, `now`, the
    /// persisted dismissals, and the live recording flag. Returns content ONLY when
    /// ALL hold (R11/R12/AE4):
    ///   1. No recording is active — a live session has no gap to bridge.
    ///   2. The most recent recorded day has a resumable (named, closed) block.
    ///   3. The gap since that block ended exceeds `threshold` (default 4h).
    ///   4. That (day, thread) has not been dismissed.
    /// The most recent recorded day wins: if its block is dismissed, the card is
    /// suppressed (it does NOT fall through to an older day the user already saw).
    static func shouldShowResumeCard(
        days: [TasksQueryDay],
        now: Date = Date(),
        dismissals: Set<String>,
        isRecording: Bool = false,
        threshold: TimeInterval = ResumeCardModel.resumeGapThreshold,
        calendar: Calendar = .current
    ) -> ResumeCardContent? {
        // (1) Mid-session: an active recording is the opposite of a resume moment.
        if isRecording { return nil }
        // (2) The most recent recorded day with a resumable stopping block, and that
        // block (its LAST, by start order). No fall-through past it: if the newest
        // thread is still fresh, there is NO gap to bridge — we never nag about an
        // older day when the user's latest work is recent.
        guard let stop = mostRecentStop(in: days, calendar: calendar) else { return nil }
        // (3) Gap gate on the last resumable activity — enough wall-clock must have
        // elapsed since that block ended (default 4h).
        let ended = Date(timeIntervalSince1970: stop.block.endTs)
        guard now.timeIntervalSince(ended) >= threshold else { return nil }
        let identity = stop.block.threadId ?? stop.block.blockId ?? stop.block.name
        let content = ResumeCardContent(
            day: stop.dayDate,
            dayLabel: DaysModel.label(for: stop.dayDate, now: now, calendar: calendar),
            threadName: stop.block.name,
            blockId: stop.block.blockId,
            startMs: Int((stop.block.startTs * 1000).rounded()),
            endMs: Int((stop.block.endTs * 1000).rounded()),
            stoppingClock: TasksModel.clock(stop.block.endTs),
            dismissalKey: dismissalKey(day: stop.day.date, threadIdentity: identity)
        )
        // (4) Dismissed (day, thread) suppresses re-show for that pair only.
        if dismissals.contains(content.dismissalKey) { return nil }
        return content
    }

    /// The most recent recorded day that has a resumable stopping block, that day's
    /// parsed local Date, and the block itself (the LAST resumable block by start
    /// order). `days` is reverse-chronological (newest first, daemon-guaranteed);
    /// a day with no named/closed block — or a malformed date — is skipped. A
    /// stopping block is never the honest-blank/unnamed state or the live open block
    /// (R2/R11), so the card always names real, left-behind work.
    private static func mostRecentStop(
        in days: [TasksQueryDay],
        calendar: Calendar
    ) -> (day: TasksQueryDay, dayDate: Date, block: TasksQueryTask)? {
        for day in days {
            guard let dayDate = TasksModel.parseDay(day.date, calendar: calendar) else { continue }
            guard let block = day.tasks.last(where: isResumable) else { continue }
            return (day, dayDate, block)
        }
        return nil
    }

    /// A block is a resumable stopping point when it carries a real work name (never
    /// the honest unnamed/blank state, R2) and is not the live open block (a session
    /// still in progress is not a gap the user left, R11).
    private static func isResumable(_ task: TasksQueryTask) -> Bool {
        !task.isOpen
            && !task.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

/// The dismissible resume card (mirrors `ShellSidebar.localModelHint`'s teal-tinted
/// dismissible card): a title, the thread name + stopping point, a primary "Jump
/// back in" action (→ the day at the thread's last block), and an [x] that persists
/// the (day, thread) dismissal. In-app only — no macOS notifications (R12).
struct ResumeCardView: View {
    let content: ResumeCardContent
    /// Jump to the day at the thread's last block (by `block_id` span, KTD-2).
    var onResume: () -> Void
    /// Persist the (day, thread) dismissal and hide the card (R11 — a new day
    /// re-arms it via a different key).
    var onDismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Picking up from \(content.dayLabel)")
                    .font(SCTypography.sans(size: 12, weight: .semibold))
                    .foregroundStyle(Color.scInkMuted)
                Text(content.threadName)
                    .font(SCTypography.sans(size: 15, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                    .fixedSize(horizontal: false, vertical: true)
                Text("You left off at \(content.stoppingClock)")
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkSecondary)
                Button(action: onResume) {
                    Text("Jump back in →")
                        .font(SCTypography.sans(size: 12.5, weight: .semibold))
                        .foregroundStyle(Color.scTeal)
                }
                .buttonStyle(.plain)
                .padding(.top, 2)
                .accessibilityHint("Opens the day where you left off")
            }
            Spacer(minLength: 0)
            Button(action: onDismiss) {
                Image(systemName: "xmark").font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.scInkMuted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .padding(14)
        .background(Color.scTeal.opacity(0.08), in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(Color.scTeal.opacity(0.25), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Picking up from \(content.dayLabel), \(content.threadName), left off at \(content.stoppingClock)")
    }
}
