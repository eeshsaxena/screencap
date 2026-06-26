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
    // SCR-177 U4 — the matched terms to bold in the snippet, and the shared
    // services that resolve + decode the leading thumbnail. Optional/defaulted so
    // a row can still render (placeholder) without them.
    var queryTerms: [String] = []
    var frameIndex: RecordingFrameIndex?
    var thumbnailLoader: ThumbnailLoader?

    /// The decoded thumbnail tagged with the pointer key it was loaded for. The
    /// cell only trusts it when its key matches the current row (mirrors
    /// `ScreenshotTruthPane`'s `loaded?.url == url` gate) so a reused row never
    /// paints the previous recording's frame. `image == nil` = resolved but no
    /// frame (the miss placeholder).
    @State private var loaded: LoadedThumb?

    private struct LoadedThumb {
        let key: String
        let image: ThumbnailImage?
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            thumbnailCell
            VStack(alignment: .leading, spacing: 2) {
                Text(primaryText)
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
        // Re-keys on the pointer so List row-reuse reloads the right frame. On
        // scroll-away the task is cancelled: the post-`await` `isCancelled` guards
        // drop the stale state write, and a not-yet-started decode is skipped
        // (ThumbnailLoader bails on a cancelled caller). A decode already in flight
        // is detached and runs to completion, but its result is cached for reuse
        // (mirrors ScreenshotTruthPane).
        .task(id: thumbnailLoadKey) { await loadThumbnail() }
    }

    /// Bold the matched query terms in content/audio snippets; activity rows have
    /// no snippet to highlight.
    private var primaryText: AttributedString {
        switch item.stream {
        case .activity: return AttributedString(item.primaryText)
        case .screen, .audio: return SnippetHighlighter.attributed(item.primaryText, terms: queryTerms)
        }
    }

    /// ~16:9 leading cell; the loaded frame replaces the stream-icon column, the
    /// miss state reuses the slot for the icon. Accessibility-hidden — it is a
    /// visual recognition aid; the Button already announces the row's text + time.
    @ViewBuilder
    private var thumbnailCell: some View {
        Group {
            // Only trust `loaded` when it matches this row's current pointer; a
            // stale (reused-row) or not-yet-loaded state shows the placeholder.
            if loaded?.key == thumbnailLoadKey, let resolved = loaded {
                if let image = resolved.image {
                    Image(decorative: image.cgImage, scale: 1)
                        .resizable()
                        .aspectRatio(contentMode: .fill)
                } else {
                    Rectangle()
                        .fill(Color(nsColor: .separatorColor).opacity(0.4))
                        .overlay {
                            Image(systemName: item.streamIcon).foregroundStyle(item.streamTint)
                        }
                }
            } else {
                Rectangle().fill(Color(nsColor: .separatorColor))
            }
        }
        .frame(width: 56, height: 32)
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .accessibilityHidden(true)
    }

    private var thumbnailLoadKey: String {
        "\(item.recording)|\(item.anchorMs.map(String.init) ?? "nil")"
    }

    /// Resolve the pointer to a frame and decode its thumbnail, tagging the result
    /// with the key it was loaded for. A `nil` anchor or an over-stale snap
    /// resolves to `nil` in U1 → the miss placeholder (never an arbitrary
    /// frame-0); an unreadable frame → miss too (R5). Every write after an
    /// `await` is cancellation-guarded so a reused row's cancelled load cannot
    /// clobber the new item's state.
    private func loadThumbnail() async {
        let key = thumbnailLoadKey
        guard let frameIndex, let thumbnailLoader else {
            loaded = LoadedThumb(key: key, image: nil)
            return
        }
        let url = await frameIndex.resolve(recording: item.recording, anchorMs: item.anchorMs)
        if Task.isCancelled { return }
        guard let url else {
            loaded = LoadedThumb(key: key, image: nil)
            return
        }
        let image = await thumbnailLoader.thumbnail(for: url)
        if Task.isCancelled { return }
        loaded = LoadedThumb(key: key, image: image)
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
