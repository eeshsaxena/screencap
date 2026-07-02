import SwiftUI

/// What the calendar detail's centered hero communicates while the user has no
/// finalized recordings (`RecordingsIndex.totalCount == 0`). Pure so the branch
/// is unit-testable — the SwiftUI body itself is not (mirrors how
/// `FirstRunSetupPresentationPolicy` is factored out of `MainWindow`).
///
/// The recording case exists because during a user's *first* recording the
/// index has not refreshed yet (it refreshes only on `recording_finalized`), so
/// `totalCount` stays 0 the whole time. Without this branch the "click Start"
/// welcome keeps dominating the window and contradicts the active recording —
/// the "it's not clear that the recording started" report.
enum EmptyStateHero: Equatable {
    /// No recording in flight — invite the user to start their first capture.
    case welcome
    /// A recording is active (starting / recording / stopping) — make the
    /// dominant visual say so. Carries the state-appropriate headline.
    case recording(headline: String)

    static func resolve(state: RecordingState) -> EmptyStateHero {
        switch state {
        case .idle:
            return .welcome
        case .starting:
            return .recording(headline: "Starting…")
        case .recording:
            return .recording(headline: "Recording in progress")
        case .stopping:
            return .recording(headline: "Finishing up…")
        }
    }
}

/// Month-grid calendar with dot-density heat map (DL-007: 4 tiers via opacity).
/// Today is highlighted via accent color. Empty welcome state replaces the grid
/// when `RecordingsIndex.totalCount == 0` — and while a recording is active that
/// welcome becomes an active-capture hero (see `EmptyStateHero`).
///
/// Day click sets the parent view's `selectedDate` and switches the content
/// area to the recordings list filtered to that day (Unit 12).
struct CalendarView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var recorder: RecorderController

    /// Drives the amber recording-dot pulse in the active-capture hero. Toggled
    /// on `.onAppear` so SwiftUI sees a value change and starts the repeating
    /// animation (same technique as `RecordingBanner`).
    @State private var pulsing = false

    @Binding var selectedDate: Date?
    @Binding var visibleMonth: Date

    /// Called when the user clicks a day with recordings or the "Recordings"
    /// affordance — parent transitions to RecordingsListView.
    var onSelectDay: (Date?) -> Void

    private var calendar: Calendar { .current }

    var body: some View {
        if index.totalCount == 0 {
            emptyState
        } else {
            populatedState
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        switch EmptyStateHero.resolve(state: recorder.state) {
        case .welcome:
            welcomeHero
        case .recording(let headline):
            recordingHero(headline: headline)
        }
    }

    /// First-run invitation. Only shown while idle now — the active-capture case
    /// owns the recording presentation, so the Start button no longer needs a
    /// disabled state (it is never on screen mid-recording).
    private var welcomeHero: some View {
        VStack(spacing: 16) {
            Image(systemName: "record.circle")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)
            Text("Record your screen with privacy built in")
                .font(.title2.bold())
            Text("Click Start to capture your first recording. Sensitive apps are masked automatically.")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
            Button("Start Recording") { recorder.start() }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    /// Active-capture hero. Replaces the "click Start" welcome the moment a
    /// recording begins so the *dominant* visual in the window states recording
    /// is live. The thin `RecordingBanner` and menubar glyph remain, but they
    /// are peripheral; this is what the user's eye lands on.
    private func recordingHero(headline: String) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "record.circle.fill")
                .font(.system(size: 48))
                // Warm-amber recording role (R7) — same signal as the banner dot,
                // distinguished from error (red triangle) by hue and shape.
                .foregroundStyle(Color.scRecording)
                .opacity(pulsing ? 1.0 : 0.4)
                .animation(.easeInOut(duration: 1.0).repeatForever(autoreverses: true), value: pulsing)
                .onAppear { pulsing = true }
                .onDisappear { pulsing = false }
            Text(headline)
                .font(.title2.bold())
            Text("ScreenCap is capturing your screen. Sensitive apps are masked automatically.")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
            Button("Stop Recording") { recorder.stop() }
                .buttonStyle(AuroraButtonStyle())
                // `stop()` only acts on `.recording`; disable during `.starting`
                // and `.stopping` so the click can't silently no-op (mirrors the
                // banner's Stop guard).
                .disabled({
                    switch recorder.state {
                    case .starting, .stopping: return true
                    case .recording, .idle: return false
                    }
                }())
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }

    private var populatedState: some View {
        VStack(alignment: .leading, spacing: 16) {
            monthHeader
            weekdayHeader
            grid
        }
        .padding(20)
    }

    private var monthHeader: some View {
        HStack {
            Button { shiftMonth(by: -1) } label: {
                Image(systemName: "chevron.left")
            }
            .buttonStyle(.borderless)

            Text(monthTitle)
                .font(.title3.bold())
                .frame(minWidth: 180)

            Button { shiftMonth(by: 1) } label: {
                Image(systemName: "chevron.right")
            }
            .buttonStyle(.borderless)
            .disabled(isCurrentOrFutureMonth)

            Spacer()

            Button("Today") { visibleMonth = startOfCurrentMonth() }
                .buttonStyle(.borderless)
                .disabled(calendar.isDate(visibleMonth, equalTo: Date(), toGranularity: .month))
        }
    }

    private var weekdayHeader: some View {
        let symbols = calendar.shortStandaloneWeekdaySymbols
        return HStack(spacing: 4) {
            ForEach(symbols, id: \.self) { sym in
                Text(sym)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    private var grid: some View {
        let columns = Array(repeating: GridItem(.flexible(), spacing: 4), count: 7)
        return LazyVGrid(columns: columns, spacing: 4) {
            ForEach(daysToRender, id: \.self) { day in
                if let day {
                    dayCell(for: day)
                } else {
                    Color.clear.frame(height: 56)
                }
            }
        }
    }

    @ViewBuilder
    private func dayCell(for day: Date) -> some View {
        let count = index.countsByDay[day] ?? 0
        let isToday = calendar.isDateInToday(day)
        let isFuture = day > Date()
        let isSelected = selectedDate == day

        Button {
            if !isFuture { onSelectDay(day) }
        } label: {
            VStack(spacing: 4) {
                Text("\(calendar.component(.day, from: day))")
                    .font(.callout)
                    .foregroundColor(isFuture ? Color.secondary.opacity(0.5) : (isToday ? Color.accentColor : Color.primary))
                dotIndicator(count: count)
                    .frame(height: 10)
            }
            .frame(maxWidth: .infinity, minHeight: 56)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(isToday ? Color.accentColor : .clear, lineWidth: 1)
            )
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(isSelected ? Color.accentColor.opacity(0.12) : .clear)
            )
        }
        .buttonStyle(.plain)
        .disabled(isFuture)
    }

    @ViewBuilder
    private func dotIndicator(count: Int) -> some View {
        switch count {
        case 0:
            EmptyView()
        case 1:
            Circle().fill(Color.accentColor.opacity(0.40)).frame(width: 6, height: 6)
        case 2...5:
            Circle().fill(Color.accentColor.opacity(0.70)).frame(width: 6, height: 6)
        default:
            VStack(spacing: 1) {
                Circle().fill(Color.accentColor).frame(width: 6, height: 6)
                Text("···")
                    .font(.system(size: 7))
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Month math

    private static let monthTitleFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMMM yyyy"
        return f
    }()

    private var monthTitle: String {
        Self.monthTitleFormatter.string(from: visibleMonth)
    }

    private var isCurrentOrFutureMonth: Bool {
        calendar.compare(visibleMonth, to: Date(), toGranularity: .month) != .orderedAscending
    }

    private func startOfCurrentMonth() -> Date {
        let comps = calendar.dateComponents([.year, .month], from: Date())
        return calendar.date(from: comps) ?? Date()
    }

    private func shiftMonth(by months: Int) {
        if let next = calendar.date(byAdding: .month, value: months, to: visibleMonth) {
            visibleMonth = next
        }
    }

    /// Days in the visible month, padded at the start with `nil`s so the first
    /// day lands on the correct weekday column.
    private var daysToRender: [Date?] {
        let comps = calendar.dateComponents([.year, .month], from: visibleMonth)
        guard
            let firstOfMonth = calendar.date(from: comps),
            let range = calendar.range(of: .day, in: .month, for: firstOfMonth)
        else {
            return []
        }
        let leadingEmpty = (calendar.component(.weekday, from: firstOfMonth) - calendar.firstWeekday + 7) % 7
        var cells: [Date?] = Array(repeating: nil, count: leadingEmpty)
        for offset in 0..<range.count {
            if let d = calendar.date(byAdding: .day, value: offset, to: firstOfMonth) {
                cells.append(calendar.startOfDay(for: d))
            }
        }
        return cells
    }
}
