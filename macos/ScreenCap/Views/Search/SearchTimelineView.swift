import SwiftUI

// SCR-174 U5 — result row + per-result display helpers for the in-app Search
// results. The results themselves render as a `List` with per-day `Section`s
// directly in `SearchView` (a List that is the detail root sizes reliably;
// nesting a List/ScrollView below siblings in a VStack inside the
// NavigationSplitView detail does not — it starves every sibling of height).

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
    /// Hoisted so a row render doesn't allocate a DateFormatter per cell.
    private static let timeLabelFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

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
        return Self.timeLabelFormatter.string(from: Date(timeIntervalSince1970: Double(anchorMs) / 1000))
    }
}
