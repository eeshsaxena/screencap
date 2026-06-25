import SwiftUI

// SCR-174 U5 — renders anchored search results as a per-day timeline: a day
// strip (days containing hits, defaulting to the most recent) over a
// chronological list of result cards for the selected day. Selecting a card
// opens the Review window at that moment (wired in U6).
struct SearchTimelineView: View {
    let items: [SearchResultItem]  // anchored only (anchorMs != nil)
    let onOpen: (SearchResultItem) -> Void

    @State private var selectedDay: Date?

    private var grouped: [(day: Date, items: [SearchResultItem])] {
        let groups = Dictionary(grouping: items) { item -> Date in
            let secs = Double(item.anchorMs ?? 0) / 1000
            return Calendar.current.startOfDay(for: Date(timeIntervalSince1970: secs))
        }
        return groups
            .map { (day: $0.key, items: $0.value.sorted { ($0.anchorMs ?? 0) > ($1.anchorMs ?? 0) }) }
            .sorted { $0.day > $1.day }
    }

    private var activeDay: Date? { selectedDay ?? grouped.first?.day }

    private var itemsForActiveDay: [SearchResultItem] {
        guard let activeDay else { return [] }
        return grouped.first { $0.day == activeDay }?.items ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if grouped.count > 1 {
                dayStrip
                Divider()
            }
            List(itemsForActiveDay) { item in
                Button { onOpen(item) } label: { ResultRow(item: item) }
                    .buttonStyle(.plain)
                    .contentShape(Rectangle())
            }
            .listStyle(.inset)
        }
    }

    private var dayStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(grouped, id: \.day) { group in
                    let isActive = group.day == activeDay
                    Button { selectedDay = group.day } label: {
                        Text(Self.dayLabel(group.day))
                            .font(.callout)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(
                                isActive ? Color.accentColor.opacity(0.18) : Color.clear,
                                in: Capsule()
                            )
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }

    static func dayLabel(_ day: Date) -> String {
        if Calendar.current.isDateInToday(day) { return "Today" }
        if Calendar.current.isDateInYesterday(day) { return "Yesterday" }
        let f = DateFormatter()
        f.dateFormat = "EEE MMM d"
        return f.string(from: day)
    }
}

private struct ResultRow: View {
    let item: SearchResultItem

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: item.streamIcon)
                .foregroundStyle(item.streamTint)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.primaryText)
                    .lineLimit(2)
                if let secondary = item.secondaryText {
                    Text(secondary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 2) {
                Text(item.timeLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if item.approximate {
                    Text("≈ audio")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

extension SearchResultItem {
    var streamIcon: String {
        switch stream {
        case .screen: return "text.viewfinder"
        case .audio: return "waveform"
        case .activity: return "macwindow"
        }
    }

    var streamTint: Color {
        switch stream {
        case .screen: return .blue
        case .audio: return .purple
        case .activity: return .green
        }
    }

    /// Lead line: the on-screen/audio snippet, or the app for an activity row.
    var primaryText: String {
        switch stream {
        case .activity: return app ?? "Activity"
        case .screen, .audio: return (snippet?.isEmpty == false ? snippet! : "(no preview)")
        }
    }

    var secondaryText: String? {
        switch stream {
        case .activity: return title
        case .screen: return "On screen"
        case .audio: return "Heard in audio"
        }
    }

    var timeLabel: String {
        guard let anchorMs else { return "—" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: Date(timeIntervalSince1970: Double(anchorMs) / 1000))
    }
}
