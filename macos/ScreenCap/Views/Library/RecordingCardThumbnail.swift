import SwiftUI

/// The shared card thumbnail (U5 Library, U8 Journal): a rounded first-frame
/// image with a bottom-right mono duration chip, falling back to the hatched
/// placeholder when the recording has no readable frame. Loads its own
/// thumbnail via the shared frame index + cache; the parent supplies aspect
/// ratio and border (hover styling stays per-screen).
struct RecordingCardThumbnail: View {
    let recording: RecordingSummary
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    /// 16/9.6 for Library cards (design 363), 16/9 for Journal cards (405).
    var aspectRatio: CGFloat
    var borderColor: Color

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

    var body: some View {
        content
            .frame(maxWidth: .infinity)
            .aspectRatio(aspectRatio, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(borderColor, lineWidth: 1)
            )
            .overlay(alignment: .bottomTrailing) { durationChip }
            .task(id: recording.stableID) { await loadThumbnail() }
    }

    @ViewBuilder
    private var content: some View {
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

    private func loadThumbnail() async {
        let key = recording.stableID
        if let url = await frameIndex.firstFrameURL(recording: recording.name) {
            let image = await thumbnailLoader.thumbnail(for: url)
            if Task.isCancelled { return }
            loaded = Loaded(key: key, image: image)
            return
        }
        // No flat screenshot frame — the default capture records video, not flat
        // frames, so most finished recordings land here. Fall back to a poster
        // frame from the local video chunk so the card previews the recording
        // instead of a permanent blank hatch; nil (no local video) keeps the
        // placeholder (R5).
        if Task.isCancelled { return }
        let poster = await frameIndex.posterFrame(recording: recording.name)
        if Task.isCancelled { return }
        loaded = Loaded(key: key, image: poster)
    }
}
