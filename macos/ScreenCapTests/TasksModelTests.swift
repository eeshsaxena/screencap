import XCTest
@testable import ScreenCap

/// U6 — the Tasks surface's pure model (`TasksModel`): cross-day grouping, the
/// LOCAL substring filter, the filter-zero-vs-system-zero distinction (R21), and
/// the honest zero/degraded state mapping from the per-recording rollup. Also pins
/// the `tasks.query` wire decoding. Rendering is verified by build-and-run.
final class TasksModelTests: XCTestCase {

    // MARK: - Wire decoding

    func testDecodesTasksQueryResponse() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.0.0",
          "api_schema_version": 1, "start_date": "2026-07-01", "end_date": "2026-07-18",
          "days": [
            {"date": "2026-07-18", "tasks": [
              {"recording": "rec-a", "recording_id": "rid-a", "task_index": 3,
               "start_ts": 100.0, "end_ts": 260.0, "name": "Mixing in Ableton",
               "category": "audio", "confidence": "high"}
            ]},
            {"date": "2026-07-17", "tasks": [
              {"recording": "rec-b", "task_index": 0, "start_ts": 50.0, "end_ts": 90.0,
               "name": "Email triage", "category": null, "confidence": null}
            ]}
          ],
          "recordings": [
            {"name": "rec-a", "recording_id": "rid-a", "state": "ready",
             "reason": "produced_tasks", "detail": null},
            {"name": "rec-b", "state": "ready", "reason": "nothing_to_name", "detail": null}
          ],
          "store_state": "mounted"
        }
        """
        let resp = try JSONDecoder().decode(TasksQueryResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.days.count, 2)
        XCTAssertEqual(resp.days[0].date, "2026-07-18")
        XCTAssertEqual(resp.days[0].tasks[0].name, "Mixing in Ableton")
        XCTAssertEqual(resp.days[0].tasks[0].taskIndex, 3)
        XCTAssertEqual(resp.days[0].tasks[0].recordingId, "rid-a")
        XCTAssertEqual(resp.recordings.count, 2)
        XCTAssertEqual(resp.recordings[1].reason, "nothing_to_name")
        XCTAssertTrue(resp.resolvedStoreState.isMounted)
    }

    /// An older daemon that omits `store_state` / `recordings` decodes tolerantly.
    func testDecodesToleratesMissingOptionalFields() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0",
         "api_schema_version": 1, "start_date": "2026-07-01", "end_date": "2026-07-02",
         "days": []}
        """
        let resp = try JSONDecoder().decode(TasksQueryResponse.self, from: Data(json.utf8))
        XCTAssertTrue(resp.days.isEmpty)
        XCTAssertTrue(resp.recordings.isEmpty)
        XCTAssertTrue(resp.resolvedStoreState.isMounted)  // absent → mounted
    }

    func testSealedStoreDecodesDegraded() throws {
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.0.0",
         "api_schema_version": 1, "start_date": "2026-07-01", "end_date": "2026-07-02",
         "days": [], "recordings": [], "store_state": "locked"}
        """
        let resp = try JSONDecoder().decode(TasksQueryResponse.self, from: Data(json.utf8))
        XCTAssertFalse(resp.resolvedStoreState.isMounted)
        XCTAssertEqual(resp.resolvedStoreState, .locked)
    }

    // MARK: - Grouping (reverse-chron preserved, KTD-11 day rule)

    func testDayGroupsPreserveOrderAndMapRows() {
        let cal = Calendar.current
        let now = cal.startOfDay(for: Date())
        let today = TasksModel.dateKey(now, calendar: cal)
        let yesterday = TasksModel.dateKey(cal.date(byAdding: .day, value: -1, to: now)!, calendar: cal)
        let days = [
            TasksQueryDay(date: today, tasks: [
                task("rec-a", 0, name: "Alpha", startTs: 1000, endTs: 1600),
                task("rec-a", 1, name: "Beta", startTs: 2000, endTs: 2600),
            ]),
            TasksQueryDay(date: yesterday, tasks: [
                task("rec-b", 0, name: "Gamma", startTs: 500, endTs: 900),
            ]),
        ]
        let groups = TasksModel.dayGroups(from: days, now: Date(), calendar: cal)
        XCTAssertEqual(groups.count, 2)
        XCTAssertEqual(groups[0].label, "Today")
        XCTAssertEqual(groups[0].rows.map(\.name), ["Alpha", "Beta"])
        XCTAssertEqual(groups[1].label, "Yesterday")
        XCTAssertEqual(groups[1].rows.map(\.name), ["Gamma"])
        // Row pointer fields ride for seek + curation (never rendered).
        XCTAssertEqual(groups[0].rows[0].recording, "rec-a")
        XCTAssertEqual(groups[0].rows[0].taskIndex, 0)
        // Seek + highlight are absolute unix ms.
        XCTAssertEqual(groups[0].rows[0].startMs, 1_000_000)
        XCTAssertEqual(groups[0].rows[0].highlight, DaySpanHighlight(startMs: 1_000_000, endMs: 1_600_000))
        // Day maps to the local start-of-day (KTD-11).
        XCTAssertEqual(groups[0].rows[0].day, now)
    }

    func testMalformedDateIsSkippedNeverFabricated() {
        let groups = TasksModel.dayGroups(
            from: [TasksQueryDay(date: "not-a-date", tasks: [task("r", 0, name: "X", startTs: 1, endTs: 2)])]
        )
        XCTAssertTrue(groups.isEmpty)
    }

    func testTimeRangeCollapsesSubMinuteSpan() {
        let rows = TasksModel.dayGroups(
            from: [TasksQueryDay(date: "2026-07-18", tasks: [
                task("r", 0, name: "Multi", startTs: 3600, endTs: 7200),
                task("r", 1, name: "Instant", startTs: 3600, endTs: 3620),
            ])]
        ).first!.rows
        XCTAssertTrue(rows[0].timeRangeText.contains("–"))   // a real span shows a range
        XCTAssertFalse(rows[1].timeRangeText.contains("–"))  // same HH:mm collapses
    }

    // MARK: - Local filter (browse data only)

    func testFilterMatchesNameAndCategoryCaseInsensitively() {
        let groups = sampleGroups()
        XCTAssertEqual(TasksModel.filter(groups, query: "mix").flatMap { $0.rows.map(\.name) }, ["Mixing"])
        // Category match.
        XCTAssertEqual(TasksModel.filter(groups, query: "AUDIO").flatMap { $0.rows.map(\.name) }, ["Mixing"])
        // Empty query returns groups unchanged.
        XCTAssertEqual(TasksModel.filter(groups, query: "   ").count, groups.count)
    }

    func testFilterDropsDaysLeftEmpty() {
        let groups = sampleGroups()  // day1: Mixing (audio); day2: Email triage (nil cat)
        let filtered = TasksModel.filter(groups, query: "mix")
        XCTAssertEqual(filtered.count, 1)
        XCTAssertEqual(filtered[0].rows.map(\.name), ["Mixing"])
    }

    // MARK: - listState: filter-zero vs system-zero (R21)

    func testListStatePopulatedWithoutQuery() {
        let state = TasksModel.listState(
            days: sampleDays(), recordings: [], query: "", verdict: usable()
        )
        guard case .populated(let groups) = state else { return XCTFail("expected populated") }
        XCTAssertEqual(groups.count, 2)
    }

    /// Tasks EXIST but the filter matches none → filter-zero, NOT the system-wide
    /// zero copy (which would misread as data loss).
    func testListStateFilterZeroWhenTasksExistButNoneMatch() {
        let state = TasksModel.listState(
            days: sampleDays(), recordings: [], query: "zzz-nomatch", verdict: usable()
        )
        guard case .filterZero(let q) = state else {
            return XCTFail("expected filterZero, got \(state)")
        }
        XCTAssertEqual(q, "zzz-nomatch")
    }

    /// No tasks loaded at all → system-zero even with a query in the box (an empty
    /// library filtered is still an empty library, never filter-zero).
    func testListStateSystemZeroWhenNoTasksEvenWithQuery() {
        let rollup = [TasksQueryRecordingStatus(name: "r", reason: "nothing_to_name")]
        let state = TasksModel.listState(days: [], recordings: rollup, query: "anything", verdict: usable())
        guard case .systemZero(let zero) = state else {
            return XCTFail("expected systemZero, got \(state)")
        }
        XCTAssertEqual(zero.state, .nothingToName)
    }

    // MARK: - Honest system-wide zero states (R21) — never "you did nothing"

    func testSystemZeroEmptyRollupIsNothingOnFile() {
        let zero = TasksModel.systemZero(recordings: [], verdict: usable())
        XCTAssertEqual(zero.state, .unknown)
        XCTAssertFalse(zero.offersSetup)
        XCTAssertTrue(zero.title.localizedCaseInsensitiveContains("nothing on file"))
    }

    func testSystemZeroNotSetUpOffersSetup() {
        // reason nil + a NOT-usable verdict → notSetUp.
        let rollup = [TasksQueryRecordingStatus(name: "r", reason: nil)]
        let zero = TasksModel.systemZero(recordings: rollup, verdict: notUsable())
        XCTAssertEqual(zero.state, .notSetUp)
        XCTAssertTrue(zero.offersSetup)
        XCTAssertTrue(zero.title.localizedCaseInsensitiveContains("intelligence"))
    }

    func testSystemZeroCouldntRunIsRetryable() {
        let rollup = [TasksQueryRecordingStatus(name: "r", reason: "couldnt_run")]
        let zero = TasksModel.systemZero(recordings: rollup, verdict: usable())
        XCTAssertEqual(zero.state, .couldntRun)
        XCTAssertFalse(zero.offersSetup)
        XCTAssertTrue(zero.isRetryable)
    }

    func testSystemZeroNothingToNameIsQuiet() {
        let rollup = [TasksQueryRecordingStatus(name: "r", reason: "nothing_to_name")]
        let zero = TasksModel.systemZero(recordings: rollup, verdict: usable())
        XCTAssertEqual(zero.state, .nothingToName)
        XCTAssertFalse(zero.offersSetup)
        XCTAssertFalse(zero.isRetryable)
    }

    /// Aggregation is most-actionable-first: an un-set-up recording alongside a
    /// benign "nothing to name" reads "set up intelligence", not the quiet state.
    func testAggregateHonestStatePrefersMostActionable() {
        let rollup = [
            TasksQueryRecordingStatus(name: "a", reason: "nothing_to_name"),
            TasksQueryRecordingStatus(name: "b", reason: nil),  // → notSetUp under not-usable verdict
        ]
        XCTAssertEqual(
            TasksModel.aggregateHonestState(recordings: rollup, verdict: notUsable()),
            .notSetUp
        )
    }

    /// An unresolved verdict (settings not loaded) is "unknown", never a false
    /// "not set up" (KTD6) — so an early render doesn't nag.
    func testUnresolvedVerdictYieldsUnknownNotSetUp() {
        let rollup = [TasksQueryRecordingStatus(name: "r", reason: nil)]
        let zero = TasksModel.systemZero(recordings: rollup, verdict: nil)
        XCTAssertNotEqual(zero.state, .notSetUp)
        XCTAssertFalse(zero.offersSetup)
    }

    // MARK: - parseDay / dateKey round-trip

    func testParseDayRoundTripsWithDateKey() {
        let cal = Calendar.current
        let day = cal.startOfDay(for: Date())
        let key = TasksModel.dateKey(day, calendar: cal)
        XCTAssertEqual(TasksModel.parseDay(key, calendar: cal), day)
    }

    // MARK: - Fixtures

    private func task(
        _ recording: String, _ index: Int,
        name: String, startTs: Double, endTs: Double, category: String? = nil
    ) -> TasksQueryTask {
        TasksQueryTask(
            recording: recording, taskIndex: index,
            startTs: startTs, endTs: endTs, name: name, category: category
        )
    }

    private func sampleDays() -> [TasksQueryDay] {
        [
            TasksQueryDay(date: "2026-07-18", tasks: [
                task("rec-a", 0, name: "Mixing", startTs: 3600, endTs: 7200, category: "audio"),
            ]),
            TasksQueryDay(date: "2026-07-17", tasks: [
                task("rec-b", 0, name: "Email triage", startTs: 3600, endTs: 5400),
            ]),
        ]
    }

    private func sampleGroups() -> [TasksModel.DayGroup] {
        TasksModel.dayGroups(from: sampleDays())
    }

    private func usable() -> IntelligenceVerdict { IntelligenceVerdict(usable: true, target: .onDevice) }
    private func notUsable() -> IntelligenceVerdict { IntelligenceVerdict(usable: false, target: .none) }
}
