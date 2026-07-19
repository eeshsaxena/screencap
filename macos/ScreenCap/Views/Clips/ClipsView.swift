import AVFoundation
import AVKit
import AppKit
import SwiftUI
import UniformTypeIdentifiers

// U11 — the Clips surface (replaces the coming-soon placeholder). A browsable list
// of the durable clips the user deliberately kept (R11), read from `/v0/clip.list`.
// Each row shows the clip's SOURCE DAY + time range (never a recording name, R5),
// a thumbnail, local playback, "Export a copy" (the NSSavePanel flow), Share (the
// system share sheet, F2), and "Delete clip" behind a no-undo confirmation (clips
// are the durable artifact — R11 — so deletion gets the same rigor as range delete,
// R20). A sealed / absent / error vault is a first-class state branched BEFORE the
// empty state (KTD-20/AE8); a mounted-but-empty store gets an honest empty state
// (R21). Clips flagged `policy_purged_partial` render their flag (AE8).

/// Loads + mutates the clips catalog. `@MainActor` so `@Published` updates and the
/// NSSavePanel / share presentation stay on the main thread.
@MainActor
final class ClipsController: ObservableObject {
    enum Phase: Equatable {
        case loading
        case loaded(storeState: StoreState, clips: [ClipRecord])
        case failed(message: String)
    }

    @Published private(set) var phase: Phase = .loading
    /// A non-fatal action failure (delete, export) surfaced beside the list rather
    /// than replacing it — an honest error, never a silent no-op.
    @Published var lastActionError: String?

    /// Load the catalog. A sealed / absent / error vault comes back as a healthy
    /// `.loaded` with a degraded `storeState` + empty list (KTD-14), so the surface
    /// branches on it before the empty check. A daemon-down / decode failure lands
    /// in `.failed` with honest copy.
    func load() async {
        do {
            let response = try await DaemonClient.clipList()
            phase = .loaded(storeState: response.resolvedStoreState, clips: response.clips)
        } catch {
            phase = .failed(message: ClipsController.loadFailureMessage(error))
        }
    }

    /// Delete a clip (mp4 + catalog entry) then reload. A failure surfaces on
    /// `lastActionError`; the list is left intact.
    func deleteClip(id: String) async {
        do {
            _ = try await DaemonClient.clipDelete(id: id)
            await load()
        } catch {
            lastActionError = ClipsModel.errorMessage(error)
        }
    }

    /// Honest copy for a clip.list failure — a daemon-down state reads as "needs
    /// the background helper", everything else falls back to the shared mapper.
    static func loadFailureMessage(_ error: Error) -> String {
        if case DaemonClientError.socketUnavailable = error {
            return "Clips need the background helper — start ScreenCap's helper and try again."
        }
        if case DaemonClientError.connectionFailed = error {
            return "Clips need the background helper — start ScreenCap's helper and try again."
        }
        return ClipsModel.errorMessage(error)
    }
}

struct ClipsView: View {
    @EnvironmentObject private var store: StoreController
    @StateObject private var controller = ClipsController()

    /// The clip currently open for local playback (nil = no player sheet).
    @State private var playingClip: ClipRecord?
    /// The clip pending a no-undo delete confirmation (nil = no dialog).
    @State private var pendingDelete: ClipRecord?
    /// Drives the system share sheet for a clip file (F2).
    @State private var shareURL: URL?

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            .task { await controller.load() }
            // The no-undo delete confirmation (R11/R20) — clips are durable, so a
            // single click never deletes.
            .alert("Delete this clip?", isPresented: pendingDeletePresented, presenting: pendingDelete) { clip in
                Button("Delete clip", role: .destructive) {
                    let id = clip.id
                    pendingDelete = nil
                    Task { await controller.deleteClip(id: id) }
                }
                Button("Cancel", role: .cancel) { pendingDelete = nil }
            } message: { _ in
                Text("This can't be undone. The clip is removed from this Mac.")
            }
            // A non-fatal delete / export failure.
            .alert("Something went wrong", isPresented: actionErrorPresented, presenting: controller.lastActionError) { _ in
                Button("OK", role: .cancel) { controller.lastActionError = nil }
            } message: { message in
                Text(message)
            }
            // Local playback of a clip (never uploads — same-EUID local file).
            .sheet(item: $playingClip) { clip in
                ClipPlayerSheet(
                    url: URL(fileURLWithPath: clip.path ?? ""),
                    title: ClipsModel.dayLabel(clip.sourceDay) + " · " + ClipsModel.rangeClockText(clip),
                    onClose: { playingClip = nil }
                )
            }
            // Hidden anchor for the share sheet (F2). Upload-share stays routed
            // through the Review window (consent boundary unchanged, KTD-4).
            .background(alignment: .topLeading) {
                ShareServicePresenter(item: $shareURL)
                    .frame(width: 1, height: 1)
                    .accessibilityHidden(true)
            }
    }

    @ViewBuilder
    private var content: some View {
        switch controller.phase {
        case .loading:
            loadingState
        case .failed(let message):
            failedState(message)
        case .loaded(let storeState, let clips):
            if !storeState.isMounted {
                // KTD-20 / AE8: a sealed / absent / key-missing store is first-class,
                // branched BEFORE the empty check so it never reads as data loss.
                storeStateView(storeState)
            } else if clips.isEmpty {
                emptyState
            } else {
                populated(clips)
            }
        }
    }

    private func storeStateView(_ storeState: StoreState) -> some View {
        StoreStateView(
            storeState: storeState,
            onUnlock: { store.unlock() },
            onRetry: { Task { await controller.load() } },
            onSetup: { store.initializeStore() },
            isBusy: store.phase != .idle,
            errorText: store.lastError
        )
    }

    // MARK: - Populated

    private func populated(_ clips: [ClipRecord]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.bottom, 22)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 24) {
                    ForEach(ClipsModel.groups(clips)) { group in
                        VStack(alignment: .leading, spacing: 12) {
                            Text(group.label)
                                .font(SCTypography.serifDayHeading)
                                .foregroundStyle(Color.scInk)
                                .accessibilityAddTraits(.isHeader)
                            ForEach(group.clips) { clip in
                                ClipRowView(
                                    clip: clip,
                                    onPlay: { playClip(clip) },
                                    onShare: { shareClip(clip) },
                                    onExport: { exportCopy(clip) },
                                    onDelete: { pendingDelete = clip }
                                )
                            }
                        }
                    }
                }
                .padding(.bottom, 8)
            }
            .scrollContentBackground(.hidden)
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 28)
    }

    private var header: some View {
        HStack(alignment: .center) {
            Text("Clips")
                .font(SCTypography.screenHeading)
                .foregroundStyle(Color.scInk)
            Spacer()
        }
    }

    // MARK: - Actions

    /// Open local playback (guards a clip with no resolvable file path).
    private func playClip(_ clip: ClipRecord) {
        guard let path = clip.path, !path.isEmpty,
              FileManager.default.fileExists(atPath: path) else {
            controller.lastActionError = "That clip's file is missing on this Mac."
            return
        }
        playingClip = clip
    }

    /// Hand the clip's local file to the system share sheet (F2).
    private func shareClip(_ clip: ClipRecord) {
        guard let path = clip.path, !path.isEmpty,
              FileManager.default.fileExists(atPath: path) else {
            controller.lastActionError = "That clip's file is missing on this Mac."
            return
        }
        shareURL = URL(fileURLWithPath: path)
    }

    /// "Export a copy": pick a destination, then copy the durable clip file out of
    /// the vault (the old NSSavePanel flow). A plain file copy — the clip is
    /// already cut; no `screencap clip` re-encode.
    private func exportCopy(_ clip: ClipRecord) {
        guard let path = clip.path, !path.isEmpty else {
            controller.lastActionError = "That clip's file is missing on this Mac."
            return
        }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.mpeg4Movie]
        panel.nameFieldStringValue = suggestedFilename(clip)
        panel.canCreateDirectories = true
        panel.title = "Export a copy"
        panel.prompt = "Export"
        guard panel.runModal() == .OK, let dest = panel.url else { return }
        do {
            if FileManager.default.fileExists(atPath: dest.path) {
                try FileManager.default.removeItem(at: dest)
            }
            try FileManager.default.copyItem(at: URL(fileURLWithPath: path), to: dest)
        } catch {
            controller.lastActionError = "Couldn't export a copy. \(error.localizedDescription)"
        }
    }

    /// A day + time filename for the exported copy (never a recording name, R5).
    private func suggestedFilename(_ clip: ClipRecord) -> String {
        let range = "\(ClipsModel.clock(clip.startMs))-\(ClipsModel.clock(clip.endMs))"
            .replacingOccurrences(of: ":", with: "")
        return "clip-\(clip.sourceDay)-\(range).mp4"
    }

    // MARK: - Bindings

    private var pendingDeletePresented: Binding<Bool> {
        Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } })
    }

    private var actionErrorPresented: Binding<Bool> {
        Binding(get: { controller.lastActionError != nil }, set: { if !$0 { controller.lastActionError = nil } })
    }

    // MARK: - States

    private var emptyState: some View {
        VStack(spacing: SCMetrics.space4) {
            ShellLogoMark(size: 40)
            Text("No clips yet")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text("Select a range on a day, then choose Clip to keep a moment here. "
                + "Clips are kept encrypted on this Mac and stay even if the day's "
                + "footage is deleted or ages out.")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 380)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    private var loadingState: some View {
        VStack(spacing: SCMetrics.space3) {
            ProgressView().controlSize(.large)
            Text("Loading clips…")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func failedState(_ message: String) -> some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 34))
                .foregroundStyle(Color.scErrorFg)
            Text("Couldn't load clips")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(message)
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button("Retry") { Task { await controller.load() } }
                .padding(.top, SCMetrics.space1)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }
}

// MARK: - Clip row

/// A single clip row: thumbnail + source-day/range/duration + flags + actions.
/// Never renders a recording name (R5).
private struct ClipRowView: View {
    let clip: ClipRecord
    var onPlay: () -> Void
    var onShare: () -> Void
    var onExport: () -> Void
    var onDelete: () -> Void

    @State private var hovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            ClipThumbnail(path: clip.path, durationText: ClipsModel.durationText(clip))
                .frame(width: 132)
            VStack(alignment: .leading, spacing: 6) {
                Text(ClipsModel.rangeClockText(clip))
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
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
        .accessibilityLabel("Clip, \(ClipsModel.summaryLine(clip))")
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
            iconButton("play.fill", help: "Play this clip", action: onPlay)
            iconButton("square.and.arrow.up", help: "Share this clip", action: onShare)
            iconButton("square.and.arrow.down", help: "Export a copy", action: onExport)
            iconButton("trash", help: "Delete this clip", destructive: true, action: onDelete)
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

// MARK: - Clip thumbnail

/// A clip thumbnail: a downsampled poster extracted from the clip's mp4 via the
/// shared `RecordingFrameIndex.extractPoster`, falling back to the hatch
/// placeholder (R5). Distinct from `RecordingCardThumbnail` (keyed on a
/// `RecordingSummary` under the recordings tree) because a clip is a standalone
/// file in the `.clips/` store — but it reuses the same poster machinery and
/// visual treatment.
private struct ClipThumbnail: View {
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

// MARK: - Clip player sheet

/// Local playback of a clip in a sheet (never uploads — a same-EUID local file).
/// Owns its own `AVPlayer`, torn down on disappear so the layer's strong
/// reference is released (mirrors `AVPlayerNSView.dismantleNSView`, SCR-93).
private struct ClipPlayerSheet: View {
    let url: URL
    let title: String
    var onClose: () -> Void

    @State private var player = AVPlayer()

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title)
                    .font(SCTypography.sans(size: 13, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Spacer()
                Button("Done") { onClose() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(12)
            AVPlayerNSView(player: player)
                .frame(minWidth: 640, minHeight: 380)
        }
        .frame(minWidth: 640, minHeight: 420)
        .onAppear {
            player.replaceCurrentItem(with: AVPlayerItem(url: url))
            player.play()
        }
        .onDisappear {
            player.pause()
            player.replaceCurrentItem(with: nil)
        }
    }
}
