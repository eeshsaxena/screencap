import SwiftUI

// U5 — a single Library card (design 362–373): a 16/9.6 thumbnail with a
// bottom-right duration chip, a title, and a mono meta line + status badge. The
// card loads its own first-frame thumbnail (via the shared frame index +
// thumbnail cache), falling back to the hatched placeholder when the recording
// has no frame yet.

/// The design's diagonal hatch fill (`repeating-linear-gradient(45deg, …)`,
/// line 363) — the thumbnail placeholder when a recording has no readable frame.
/// Drawn with a `Canvas` so it scales crisply at any card width.
struct LibraryHatchPlaceholder: View {
    /// 1px lines every 11px, matching the design's `#EDE6D6 10px, 11px` stops.
    private let spacing: CGFloat = 11

    var body: some View {
        Canvas { ctx, size in
            var path = Path()
            // 45° lines sweeping across the rect: each runs from the top edge
            // down-right to the bottom edge, offset by `spacing`.
            var x = -size.height
            while x < size.width {
                path.move(to: CGPoint(x: x, y: 0))
                path.addLine(to: CGPoint(x: x + size.height, y: size.height))
                x += spacing
            }
            ctx.stroke(path, with: .color(.scFillSubtle), lineWidth: 1)
        }
        .background(Color.scCanvas)
        .accessibilityHidden(true)
    }
}

struct LibraryCard: View {
    let recording: RecordingSummary
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    /// Primary tap — opens the recording (Day timeline in U9; the read-only
    /// Inspect window until then).
    var onOpen: () -> Void
    /// Open the Review-before-upload consent window (eligible recordings only).
    var onReview: () -> Void

    /// A resolved thumbnail tagged with the recording it was loaded for, so a
    /// recycled card can't show a stale frame.
    private struct Loaded: Equatable {
        let key: String
        let image: ThumbnailImage?
        static func == (lhs: Loaded, rhs: Loaded) -> Bool {
            lhs.key == rhs.key && (lhs.image?.cgImage === rhs.image?.cgImage)
        }
    }

    @State private var loaded: Loaded?
    @State private var hovering = false

    private var badge: LibraryBadge { LibraryBadge.forRecording(recording) }

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 10) {
                thumbnail
                info
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu { contextMenu }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(recording.title), \(recording.duration), \(badge.text)")
        .task(id: recording.stableID) { await loadThumbnail() }
    }

    // MARK: - Thumbnail

    private var thumbnail: some View {
        thumbnailContent
            .frame(maxWidth: .infinity)
            .aspectRatio(16.0 / 9.6, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
            )
            .overlay(alignment: .bottomTrailing) { durationChip }
            .onHover { hovering = $0 }
    }

    @ViewBuilder
    private var thumbnailContent: some View {
        if loaded?.key == recording.stableID, let image = loaded?.image {
            Image(decorative: image.cgImage, scale: 1)
                .resizable()
                .aspectRatio(contentMode: .fill)
        } else {
            // Not-yet-loaded and resolved-miss both render the neutral hatch, so
            // the card never flashes an arbitrary frame (R5).
            LibraryHatchPlaceholder()
        }
    }

    private var durationChip: some View {
        Text(recording.duration)
            .font(SCTypography.mono(size: 10.5))
            .foregroundStyle(Color.scCanvas)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Color.scInk.opacity(0.85), in: RoundedRectangle(cornerRadius: SCMetrics.radiusHairline))
            .padding(8)
            .accessibilityHidden(true)
    }

    // MARK: - Text

    private var info: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(recording.title)
                .font(SCTypography.sans(size: 14, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .lineLimit(1)
            HStack(spacing: 8) {
                Text(metaText)
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                    .lineLimit(1)
                LibraryBadgeChip(badge: badge)
            }
        }
    }

    /// Mono meta line (design 369). The prototype's "date · person" collapses to
    /// "date · time" — there is no team/person vocabulary yet (SCR-221, KTD-9).
    private var metaText: String {
        let day = recording.startedDay.map { Self.dayFormatter.string(from: $0) } ?? recording.date
        let time = recording.startedTimeOfDay
        return time == "—" ? day : "\(day) · \(time)"
    }

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.setLocalizedDateFormatFromTemplate("d MMM")
        return f
    }()

    // MARK: - Context menu

    @ViewBuilder
    private var contextMenu: some View {
        Button("Open") { onOpen() }
        if recording.isUploadEligible {
            Divider()
            Button("Review & upload…") { onReview() }
        }
    }

    // MARK: - Loading

    private func loadThumbnail() async {
        let key = recording.stableID
        guard let url = await frameIndex.firstFrameURL(recording: recording.name) else {
            if Task.isCancelled { return }
            loaded = Loaded(key: key, image: nil)
            return
        }
        let image = await thumbnailLoader.thumbnail(for: url)
        if Task.isCancelled { return }
        loaded = Loaded(key: key, image: image)
    }
}

/// The outlined mono status chip (design 370). Border + foreground come from the
/// badge tone; the copy is honesty-substituted (KTD-9) upstream in `LibraryBadge`.
struct LibraryBadgeChip: View {
    let badge: LibraryBadge

    var body: some View {
        Text(badge.text)
            .font(SCTypography.mono(size: 10))
            .foregroundStyle(foreground)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                    .strokeBorder(border, lineWidth: 1)
            )
            .accessibilityHidden(true)
    }

    private var foreground: Color {
        switch badge.tone {
        case .uploaded: return .scTeal
        case .draft: return .scAmber
        case .local: return .scInkMuted
        }
    }

    private var border: Color {
        switch badge.tone {
        case .uploaded: return .scTeal.opacity(0.33)   // design #0E7C6B55
        case .draft: return .scAmber.opacity(0.4)      // design #D9A44166
        case .local: return .scBorderWarm
        }
    }
}
