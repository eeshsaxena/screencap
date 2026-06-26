import SwiftUI

// SCR-174 U5 — result row + per-result display helpers for the in-app Search
// results. The results themselves render as a `List` with per-day `Section`s
// directly in `SearchView` (a List that is the detail root sizes reliably;
// nesting a List/ScrollView below siblings in a VStack inside the
// NavigationSplitView detail does not — it starves every sibling of height).

/// Groups anchored results into day buckets, most-recent day first, newest
/// within each day.
func searchResultsGroupedByDay(_ items: [SearchResultItem]) -> [(day: Date, items: [SearchResultItem])] {
    let groups = Dictionary(grouping: items) { item -> Date in
        let secs = Double(item.anchorMs ?? 0) / 1000
        return Calendar.current.startOfDay(for: Date(timeIntervalSince1970: secs))
    }
    return groups
        .map { (day: $0.key, items: $0.value.sorted { ($0.anchorMs ?? 0) > ($1.anchorMs ?? 0) }) }
        .sorted { $0.day > $1.day }
}

func searchDayLabel(_ day: Date) -> String {
    if Calendar.current.isDateInToday(day) { return "Today" }
    if Calendar.current.isDateInYesterday(day) { return "Yesterday" }
    let f = DateFormatter()
    f.dateFormat = "EEE MMM d"
    return f.string(from: day)
}

struct ResultRow: View {
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
        .contentShape(Rectangle())
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
