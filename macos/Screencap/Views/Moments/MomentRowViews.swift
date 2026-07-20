import SwiftUI

// Moments — the unified row + its clip thumbnail. One list holds two row kinds
// with different physics (KTD-4): an app-detected task span (opens its day,
// rename/delete via the shared write-through) and a range the user CLIPPED
// (durable video — play / export / share / delete, plus open-day). The kinds are
// set apart by the "Clipped" marker (R3), while both share a fixed leading gutter
// so their text left-edges align even though only clipped rows carry a thumbnail.
// No recording name is ever shown (R5).

/// The fixed leading-gutter width shared by both row kinds: a clipped row fills it
/// with its thumbnail, an app-detected row reserves it empty so the two align.
private let momentRowGutter: CGFloat = 120

/// One row in the merged Moments list. Renders the app-detected or clipped shape
/// from the same `MomentRow`, invoking only the kind-appropriate action closures.
struct MomentRowView: View {
    let row: MomentsModel.MomentRow
    /// Open a task moment's dedicated scoped view — player + task-only strip, not
    /// the whole day (auto rows, R4).
    var openTask: () -> Void
    /// Jump to the row's source day (both kinds, R4).
    var openDay: () -> Void
    // Clipped-only actions.
    var play: () -> Void
    var share: () -> Void
    var exportCopy: () -> Void
    var deleteClip: () -> Void
    // App-detected-only actions.
    var rename: () -> Void
    var deleteTask: () -> Void
    // Shared thumbnail machinery for the auto row's task-span poster (KTD-6).
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader

    var body: some View {
        switch row {
        case .auto(let task):
            AutoRow(
                task: task, frameIndex: frameIndex, thumbnailLoader: thumbnailLoader,
                openTask: openTask, openDay: openDay, rename: rename, delete: deleteTask
            )
        case .clipped(let clip):
            ClippedRow(
                clip: clip, openDay: openDay, play: play,
                share: share, exportCopy: exportCopy, delete: deleteClip
            )
        }
    }
}

// MARK: - App-detected row

/// An app-detected task span: a task-span thumbnail + name + wall-clock range +
/// optional category chip. The whole row opens the task's dedicated scoped view
/// (R4); a separate "Open day →" control opens the day (AE3); curation is via the
/// shared write-through (R12). The thumbnail fills the shared gutter so its text
/// aligns with clipped rows.
private struct AutoRow: View {
    let task: TasksModel.TaskRow
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    var openTask: () -> Void
    var openDay: () -> Void
    var rename: () -> Void
    var delete: () -> Void

    @EnvironmentObject private var index: RecordingsIndex
    @State private var hovering = false

    /// Stable-id-first resolution (survives a post-stop rename) for the poster
    /// anchors + current dir name.
    private var summary: RecordingSummary? {
        index.summary(recordingId: task.recordingId, name: task.recording)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            MomentTaskThumbnail(
                task: task, summary: summary,
                frameIndex: frameIndex, thumbnailLoader: thumbnailLoader
            )
            .frame(width: momentRowGutter)
            VStack(alignment: .leading, spacing: 4) {
                Text(task.name)
                    .font(SCTypography.sans(size: 13.5, weight: .medium))
                    .foregroundStyle(Color.scInk)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                HStack(spacing: 8) {
                    Text(task.timeRangeText)
                        .font(SCTypography.mono(size: 11))
                        .foregroundStyle(Color.scInkMuted)
                    if let category = task.category, !category.isEmpty {
                        Text(category)
                            .font(SCTypography.mono(size: 10))
                            .foregroundStyle(Color.scInkMuted)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 1)
                            .overlay(
                                RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                            )
                    }
                }
            }
            Spacer(minLength: 0)
            // "Open day →" as its OWN control (event-consuming) so a click here
            // opens the day and never also fires the row's open-task.
            Button(action: openDay) {
                Text("Open day →")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
                    .opacity(hovering ? 1 : 0.6)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open the full day for this task")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onTapGesture(perform: openTask)
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Rename…", action: rename)
            Button("Delete", role: .destructive, action: delete)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.name), \(task.timeRangeText)")
        .accessibilityHint("Opens the task")
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { openTask() }
    }
}

// MARK: - Task-span thumbnail

/// A task-span thumbnail: a frame from within the task's OWN span (KTD-4), so two
/// tasks in one recording read distinctly (R2/AE3), falling back to the hatch
/// placeholder. Mirrors `MomentClipThumbnail`'s shape so auto and clipped rows
/// align; reuses the shared frame index + loader (not one per row).
private struct MomentTaskThumbnail: View {
    let task: TasksModel.TaskRow
    let summary: RecordingSummary?
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    var aspectRatio: CGFloat = 16.0 / 9.0

    @State private var image: ThumbnailImage?

    var body: some View {
        content
            .frame(maxWidth: .infinity)
            .aspectRatio(aspectRatio, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scFillSubtle, lineWidth: 1)
            )
            .task(id: task.id) { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if let image {
            Image(decorative: image.cgImage, scale: 1)
                .resizable()
                .aspectRatio(contentMode: .fill)
        } else {
            CardHatchPlaceholder()
        }
    }

    private func load() async {
        let startedAtMs = summary?.startedAt.map { Int($0 * 1000) }
        let durationMs = summary?.durationSeconds.map { Int($0 * 1000) }
        image = await frameIndex.taskThumbnail(
            // The current dir name (survives a post-stop rename), not the snapshot.
            recording: summary?.name ?? task.recording,
            taskStartMs: task.startMs,
            startedAtMs: startedAtMs,
            durationMs: durationMs,
            thumbnailLoader: thumbnailLoader
        )
    }
}

// MARK: - Clipped row

/// A range the user clipped: thumbnail + range/duration + the "Clipped" marker +
/// honesty flags, with the durable-artifact actions (play / open-day / share /
/// export / delete). The thumbnail fills the shared leading gutter.
private struct ClippedRow: View {
    let clip: ClipRecord
    var openDay: () -> Void
    var play: () -> Void
    var share: () -> Void
    var exportCopy: () -> Void
    var delete: () -> Void

    @State private var hovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            MomentClipThumbnail(path: clip.path, durationText: ClipsModel.durationText(clip))
                .frame(width: momentRowGutter)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text(ClipsModel.rangeClockText(clip))
                        .font(SCTypography.sans(size: 14, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    ClippedBadge()
                }
                Text(ClipsModel.durationText(clip))
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                flags
            }
            Spacer(minLength: 8)
            actions
        }
        .padding(14)
        .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
        )
        .onHover { hovering = $0 }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Clipped, \(ClipsModel.summaryLine(clip))")
    }

    @ViewBuilder
    private var flags: some View {
        HStack(spacing: 6) {
            if clip.honestyFlags.policyPurgedPartial {
                flagBadge(
                    ClipsModel.policyPurgedFlagText,
                    systemImage: "shield.lefthalf.filled",
                    tint: Color.scAmberText
                )
            }
            if ClipsModel.isAgentCreated(clip) {
                flagBadge("Made by an agent", systemImage: "sparkles", tint: Color.scInkMuted)
            }
        }
    }

    private func flagBadge(_ text: String, systemImage: String, tint: Color) -> some View {
        HStack(spacing: 4) {
            Image(systemName: systemImage).font(.system(size: 9, weight: .semibold))
            Text(text).font(SCTypography.mono(size: 10))
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                .strokeBorder(tint.opacity(0.4), lineWidth: 1)
        )
        .fixedSize(horizontal: false, vertical: true)
    }

    private var actions: some View {
        HStack(spacing: 6) {
            iconButton("play.fill", help: "Play this clip", action: play)
            iconButton("calendar", help: "Open this moment's day", action: openDay)
            iconButton("square.and.arrow.up", help: "Share this clip", action: share)
            iconButton("square.and.arrow.down", help: "Export a copy", action: exportCopy)
            iconButton("trash", help: "Delete this clip", destructive: true, action: delete)
        }
    }

    private func iconButton(
        _ system: String,
        help: String,
        destructive: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: system)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(destructive ? Color.scRust : Color.scInkSecondary)
                .frame(width: 28, height: 28)
                .background(Color.scCanvas, in: RoundedRectangle(cornerRadius: SCMetrics.radiusInner))
                .overlay(
                    RoundedRectangle(cornerRadius: SCMetrics.radiusInner)
                        .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(help)
    }
}

// MARK: - "Clipped" marker (R3)

/// The marker that sets a user-clipped row apart from the app-detected ones (R3).
/// Reuses the verb the user pressed — self-evident, nothing new to learn.
struct ClippedBadge: View {
    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: "scissors").font(.system(size: 8.5, weight: .semibold))
            Text("Clipped").font(SCTypography.mono(size: 9.5))
        }
        .foregroundStyle(Color.scTeal)
        .padding(.horizontal, 7)
        .padding(.vertical, 2)
        .background(Color.scTeal.opacity(0.12), in: Capsule())
        .accessibilityHidden(true)
    }
}

// MARK: - Clip thumbnail

/// A clip thumbnail: a downsampled poster from the clip's mp4 via the shared
/// `RecordingFrameIndex.extractPoster`, falling back to the hatch placeholder (R5).
/// Mirrors the Clips surface's thumbnail; reuses the same poster machinery.
private struct MomentClipThumbnail: View {
    let path: String?
    let durationText: String
    var aspectRatio: CGFloat = 16.0 / 9.0

    @State private var image: ThumbnailImage?

    var body: some View {
        content
            .frame(maxWidth: .infinity)
            .aspectRatio(aspectRatio, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scFillSubtle, lineWidth: 1)
            )
            .overlay(alignment: .bottomTrailing) { durationChip }
            .task(id: path) { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if let image {
            Image(decorative: image.cgImage, scale: 1)
                .resizable()
                .aspectRatio(contentMode: .fill)
        } else {
            CardHatchPlaceholder()
        }
    }

    private var durationChip: some View {
        Text(durationText)
            .font(SCTypography.mono(size: 10.5))
            .foregroundStyle(Color.scCanvas)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Color.scInk.opacity(0.85), in: RoundedRectangle(cornerRadius: SCMetrics.radiusHairline))
            .padding(8)
            .accessibilityHidden(true)
    }

    private func load() async {
        guard let path, !path.isEmpty else { image = nil; return }
        let poster = await RecordingFrameIndex.extractPoster(
            url: URL(fileURLWithPath: path), maxPixelSize: 320
        )
        if Task.isCancelled { return }
        image = poster
    }
}
