import SwiftUI

/// Month-grid calendar with dot-density heat map (DL-007: 4 tiers via opacity).
/// Today is highlighted via accent color. Empty welcome state replaces the grid
/// when `RecordingsIndex.totalCount == 0`.
///
/// Day click sets the parent view's `selectedDate` and switches the content
/// area to the recordings list filtered to that day (Unit 12).
struct CalendarView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var recorder: RecorderController

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

    private var emptyState: some View {
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
                .disabled(recorder.state.isRecording)
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

    private var monthTitle: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMMM yyyy"
        return formatter.string(from: visibleMonth)
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
