import XCTest
@testable import Screencap

/// U9 (day diary) — the morning resume card's PURE gating predicate
/// (`ResumeCardModel.shouldShowResumeCard`): fires only when a real thread is
/// resumable on the most recent recorded day, the gap since the last activity
/// clears the 4h threshold, and the (day, thread) pair is undismissed (R11/R12).
/// Rendering is verified by build-and-run; these pin the rules that decide WHETHER
/// and WITH WHAT the card shows — the load-bearing "only when real" logic (AE4).
final class ResumeCardModelTests: XCTestCase {

    // MARK: - Fixtures

    /// A fixed "now" — Monday 2026-07-20 09:00 local. 2026-07-17 is a Friday and
    /// 2026-07-18 a Saturday (both verified against the calendar), so the
    /// Monday-after-Friday / weekend scenarios read literally.
    private let now = ResumeCardModelTests.date(2026, 7, 20, 9, 0)

    /// Build a local Date from components in the current calendar/timezone — the
    /// same calendar `parseDay` / `DaysModel.label` resolve against, so fixtures and
    /// the predicate agree without timezone juggling.
    private static func date(_ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int) -> Date {
        var c = DateComponents()
        c.year = y; c.month = mo; c.day = d; c.hour = h; c.minute = mi
        return Calendar.current.date(from: c)!
    }

    private func ts(_ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int) -> Double {
        Self.date(y, mo, d, h, mi).timeIntervalSince1970
    }

    /// A named diary block for a day fixture (defaults model a closed, named,
    /// unthreaded block; override for threads / open / unnamed states).
    private func block(
        _ name: String,
        start: Double,
        end: Double,
        blockId: String? = nil,
        threadId: String? = nil,
        isOpen: Bool = false
    ) -> TasksQueryTask {
        TasksQueryTask(
            recording: "rec", taskIndex: 0, startTs: start, endTs: end,
            name: name, blockId: blockId, threadId: threadId, isOpen: isOpen
        )
    }

    // MARK: - AE4: Monday-after-Friday names Friday's last thread

    func testMondayAfterFridayNamesLastThread() throws {
        let email = block("Email triage", start: ts(2026, 7, 17, 9, 0), end: ts(2026, 7, 17, 9, 30),
                          blockId: "blk-fri-1")
        let kick = block("Kick drum tuning", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 24),
                         blockId: "blk-fri-2", threadId: "thr-kick")
        let days = [TasksQueryDay(date: "2026-07-17", tasks: [email, kick])]

        let content = try XCTUnwrap(
            ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: [])
        )
        // The LAST block of the most recent recorded day is the stopping point.
        XCTAssertEqual(content.threadName, "Kick drum tuning")
        XCTAssertEqual(content.blockId, "blk-fri-2")
        XCTAssertEqual(content.day, Calendar.current.startOfDay(for: Self.date(2026, 7, 17, 12, 0)))
        // Names Friday (a plain weekday label, never Today/Yesterday from Monday).
        XCTAssertEqual(content.dayLabel, DaysModel.label(for: content.day, now: now))
        XCTAssertTrue(content.dayLabel.contains("Friday"), "labels the recorded weekday")
        XCTAssertEqual(content.stoppingClock, TasksModel.clock(kick.endTs))
        // The dismissal key is (day, thread) — the thread_id when present.
        XCTAssertEqual(content.dismissalKey, "2026-07-17|thr-kick")
        // The jump span is the block's extent (deep-link by block_id span, KTD-2).
        XCTAssertEqual(content.startMs, Int((kick.startTs * 1000).rounded()))
        XCTAssertEqual(content.endMs, Int((kick.endTs * 1000).rounded()))
    }

    // MARK: - AE4: empty weekend → no card

    func testEmptyWeekendShowsNoCard() {
        // No recordings at all over the recent window.
        XCTAssertNil(ResumeCardModel.shouldShowResumeCard(days: [], now: now, dismissals: []))
        // A recorded day whose blocks are all empty is not resumable either.
        let empty = [TasksQueryDay(date: "2026-07-19", tasks: [])]
        XCTAssertNil(ResumeCardModel.shouldShowResumeCard(days: empty, now: now, dismissals: []))
    }

    // MARK: - AE4: dismissal suppresses that (day, thread); a new day re-arms

    func testDismissalSuppressesSameThread() throws {
        let kick = block("Kick drum tuning", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 24),
                         blockId: "blk-fri-2", threadId: "thr-kick")
        let days = [TasksQueryDay(date: "2026-07-17", tasks: [kick])]

        // Undismissed → shown; resolve its key, then dismiss exactly that pair.
        let shown = try XCTUnwrap(ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: []))
        XCTAssertNil(
            ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: [shown.dismissalKey]),
            "dismissing the (day, thread) pair suppresses re-show"
        )
    }

    func testNewDayReArmsAfterDismissal() {
        // Friday was dismissed; the user then recorded Saturday — the newer day's
        // key differs, so the card re-arms and names Saturday, not Friday.
        let fridayKey = ResumeCardModel.dismissalKey(day: "2026-07-17", threadIdentity: "thr-kick")
        let saturday = block("Mixing session", start: ts(2026, 7, 18, 14, 0), end: ts(2026, 7, 18, 15, 30),
                             blockId: "blk-sat-1", threadId: "thr-mix")
        let friday = block("Kick drum tuning", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 24),
                           blockId: "blk-fri-2", threadId: "thr-kick")
        // Reverse-chronological (newest first), as the daemon returns.
        let days = [
            TasksQueryDay(date: "2026-07-18", tasks: [saturday]),
            TasksQueryDay(date: "2026-07-17", tasks: [friday]),
        ]

        let content = ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: [fridayKey])
        XCTAssertEqual(content?.threadName, "Mixing session", "the most recent day wins")
        XCTAssertEqual(content?.dismissalKey, "2026-07-18|thr-mix")
    }

    // MARK: - Gap boundary (default 4h)

    func testGapBoundaryThreeHoursNoCardFiveHoursCard() {
        // A block that ended 3h before `now` (06:00) is too fresh at the 4h default.
        let fresh = block("Refactoring", start: ts(2026, 7, 20, 5, 0), end: ts(2026, 7, 20, 6, 0),
                          blockId: "blk-a")
        XCTAssertNil(
            ResumeCardModel.shouldShowResumeCard(
                days: [TasksQueryDay(date: "2026-07-20", tasks: [fresh])], now: now, dismissals: []
            ),
            "3h gap < 4h threshold → no card"
        )
        // A block that ended 5h before `now` (04:00) clears the threshold.
        let stale = block("Refactoring", start: ts(2026, 7, 20, 3, 0), end: ts(2026, 7, 20, 4, 0),
                          blockId: "blk-b")
        XCTAssertNotNil(
            ResumeCardModel.shouldShowResumeCard(
                days: [TasksQueryDay(date: "2026-07-20", tasks: [stale])], now: now, dismissals: []
            ),
            "5h gap ≥ 4h threshold → card"
        )
    }

    func testFreshMostRecentThreadDoesNotFallThroughToOlderDay() {
        // The user worked 2h ago (fresh) AND on Friday. No card: the latest thread
        // is recent, so there is no gap — never nag about Friday.
        let fresh = block("Standup notes", start: ts(2026, 7, 20, 6, 30), end: ts(2026, 7, 20, 7, 0),
                          blockId: "blk-today")
        let friday = block("Kick drum tuning", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 24),
                           blockId: "blk-fri", threadId: "thr-kick")
        let days = [
            TasksQueryDay(date: "2026-07-20", tasks: [fresh]),
            TasksQueryDay(date: "2026-07-17", tasks: [friday]),
        ]
        XCTAssertNil(ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: []))
    }

    // MARK: - Deleted day → no card (fresh computation finds nothing)

    func testDeletedDayYieldsNoCard() {
        // A since-deleted day yields no rows from the fresh query → nil by
        // construction (KTD-11: the card can never outlive its evidence).
        XCTAssertNil(ResumeCardModel.shouldShowResumeCard(days: [], now: now, dismissals: ["stale|key"]))
    }

    // MARK: - Mid-session (recording active) → no card

    func testMidSessionRecordingShowsNoCard() {
        let kick = block("Kick drum tuning", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 24),
                         blockId: "blk-fri-2", threadId: "thr-kick")
        let days = [TasksQueryDay(date: "2026-07-17", tasks: [kick])]
        XCTAssertNil(
            ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: [], isRecording: true),
            "an active recording has no gap to bridge"
        )
    }

    // MARK: - Honest stopping point: skip unnamed + live-open blocks (R2/R11)

    func testStoppingBlockSkipsUnnamedAndOpenBlocks() {
        let kick = block("Kick drum tuning", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 24),
                         blockId: "blk-named")
        // A later honest-blank (unnamed) block and a later live-open block are both
        // NOT stopping points — the card names the last REAL, closed block.
        let blank = block("  ", start: ts(2026, 7, 17, 17, 30), end: ts(2026, 7, 17, 17, 40),
                          blockId: "blk-blank")
        let open = block("Wrapping up", start: ts(2026, 7, 17, 17, 45), end: ts(2026, 7, 17, 17, 50),
                         blockId: "blk-open", isOpen: true)
        let days = [TasksQueryDay(date: "2026-07-17", tasks: [kick, blank, open])]

        let content = ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: [])
        XCTAssertEqual(content?.threadName, "Kick drum tuning")
        XCTAssertEqual(content?.blockId, "blk-named")
    }

    /// A day of only unnamed/open blocks has no resumable stopping point → no card.
    func testAllUnnamedDayShowsNoCard() {
        let blank = block("", start: ts(2026, 7, 17, 16, 0), end: ts(2026, 7, 17, 17, 0), blockId: "b1")
        let days = [TasksQueryDay(date: "2026-07-17", tasks: [blank])]
        XCTAssertNil(ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: []))
    }

    // MARK: - Lone (unthreaded) block keys by block_id

    func testLoneBlockKeysByBlockId() {
        // No thread_id → the dismissal identity falls back to the stable block_id.
        let solo = block("Reading docs", start: ts(2026, 7, 17, 10, 0), end: ts(2026, 7, 17, 11, 0),
                         blockId: "blk-solo")
        let days = [TasksQueryDay(date: "2026-07-17", tasks: [solo])]
        let content = ResumeCardModel.shouldShowResumeCard(days: days, now: now, dismissals: [])
        XCTAssertEqual(content?.dismissalKey, "2026-07-17|blk-solo")
    }

    // MARK: - Threshold constant

    func testResumeGapThresholdIsFourHours() {
        XCTAssertEqual(ResumeCardModel.resumeGapThreshold, 4 * 3600)
    }
}
