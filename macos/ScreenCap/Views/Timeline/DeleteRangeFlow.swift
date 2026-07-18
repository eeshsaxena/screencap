import SwiftUI

// U9 (R8, R19, R20, AE2) — the range-delete UX: the confirm-sheet content model,
// the Idle → Confirm → Deleting → {Idle | Error} state machine, and the confirm
// sheet view. The pure pieces (`DeleteConfirmContent`, `DeleteRangePhase`) live
// here as value types so the confirm-sheet content and the transitions unit-test
// without a render (DeleteRangeFlowTests). The daemon side is U8's
// `delete.start|status|cancel` (LOCAL-ONLY in v1); this unit consumes the
// `dry_run` preview for the confirm sheet and drives the execute job with
// progress, never a silent no-op on failure.

/// The pure content model behind the delete confirm sheet (R20): the ACTUAL
/// chunk-rounded extent the job will remove, the confirm token the execute call
/// passes back verbatim (no TOCTOU), any overlapping clips that will be KEPT, and
/// whether a live in-flight chunk was excluded (so the sheet can disclose the
/// partial honestly). Built from a `DeletePreviewResponse` (U8 `dry_run`).
struct DeleteConfirmContent: Identifiable, Equatable {
    let id = UUID()
    /// The range the user selected (absolute unix ms) — what they asked for.
    let requestedStartMs: Int
    let requestedEndMs: Int
    /// The actual removed extent after chunk-rounding, unioned across recordings.
    /// Nil when nothing resolved (no covered chunks) — the sheet then reads as a
    /// no-op-to-delete state rather than showing a false extent.
    let roundedStartMs: Int?
    let roundedEndMs: Int?
    /// Covered-chunk count across all recordings (the size of the removal).
    let totalChunks: Int
    /// How many recordings the range spans (an honest cross-recording delete).
    let recordingCount: Int
    /// Overlapping clips that will be KEPT (findable in Clips) — disclosed so
    /// "removed from this Mac" is never silently false (R20).
    let keptClips: [DeleteKeptClip]
    /// True when a live in-flight chunk overlapping the range was EXCLUDED (never
    /// deleted) — the delete is then an honest partial over past footage.
    let hasExcludedLiveChunks: Bool
    /// The confirm token: `{recording: [chunk_indices]}`, passed back verbatim to
    /// the execute call so the job deletes exactly this set.
    let resolved: [String: [Int]]

    /// Whether there is anything to delete. A range that resolves to zero covered
    /// chunks (all in a gap, or only the live chunk) is not deletable — the caller
    /// surfaces that honestly instead of firing an empty job.
    var isDeletable: Bool { totalChunks > 0 && !resolved.isEmpty }

    /// The clock extent the sheet shows (R20). Prefers the rounded extent (the
    /// truth of what will be removed); falls back to the requested range when the
    /// preview couldn't round (e.g. nothing resolved).
    var extentStartMs: Int { roundedStartMs ?? requestedStartMs }
    var extentEndMs: Int { roundedEndMs ?? requestedEndMs }

    /// "HH:mm–HH:mm" over the extent — pure so the copy is testable.
    var extentClockText: String {
        "\(Self.clock(extentStartMs))–\(Self.clock(extentEndMs))"
    }

    /// Build the confirm content from the U8 `dry_run` preview. Unions the
    /// per-recording rounded extents into one removed extent, flattens the
    /// kept-clips disclosure, and carries the confirm token forward unchanged.
    static func from(preview: DeletePreviewResponse) -> DeleteConfirmContent {
        let roundedStarts = preview.recordings.compactMap(\.roundedStartMs)
        let roundedEnds = preview.recordings.compactMap(\.roundedEndMs)
        return DeleteConfirmContent(
            requestedStartMs: preview.startMs,
            requestedEndMs: preview.endMs,
            roundedStartMs: roundedStarts.min(),
            roundedEndMs: roundedEnds.max(),
            totalChunks: preview.totalChunks,
            recordingCount: preview.recordings.filter { !$0.chunkIndices.isEmpty }.count,
            keptClips: preview.recordings.flatMap(\.keptClips),
            hasExcludedLiveChunks: preview.recordings.contains { !$0.excludedLiveChunks.isEmpty },
            resolved: preview.resolved
        )
    }

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    static func clock(_ ms: Int) -> String {
        clockFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }
}

/// U9 — the delete state machine (the plan's U7 range-gesture diagram tail:
/// Confirm → Deleting → {Idle | Error}). Pure so the transitions unit-test.
///
///   idle → confirming (dry_run preview loaded)
///   confirming → deleting (user confirmed; AVPlayer torn down; execute fired)
///   deleting → idle (job completed; strip reloads)
///   deleting → failed (job failed / cancelled / reconfirm_required — never silent)
///   confirming | failed → idle (dismissed)
enum DeleteRangePhase: Equatable {
    case idle
    case confirming(DeleteConfirmContent)
    case deleting(fraction: Double?)
    case failed(message: String)

    /// The confirm content while confirming, else nil.
    var confirmContent: DeleteConfirmContent? {
        if case .confirming(let content) = self { return content }
        return nil
    }

    /// The determinate progress fraction while deleting (nil = indeterminate).
    var deletingFraction: Double? {
        if case .deleting(let fraction) = self { return fraction }
        return nil
    }

    /// Whether the confirm/progress sheet should be up (confirming OR deleting) —
    /// one sheet morphs from the confirm content into the progress view so the
    /// transition reads as one continuous action.
    var sheetPresented: Bool {
        switch self {
        case .confirming, .deleting: return true
        case .idle, .failed: return false
        }
    }

    /// The error message while failed, else nil (drives the writeError-style alert).
    var failureMessage: String? {
        if case .failed(let message) = self { return message }
        return nil
    }
}

/// U9 — plain, honest copy for a failed / cancelled / reconfirm-required delete
/// job (R19: never silent). Pure so the wording is testable; mirrors the
/// `DayTasks.describe` mapping style for typed daemon envelope codes.
enum DeleteRangeFailure {
    /// The re-confirm-required message (KTD-3 no-TOCTOU): the resolved chunk set
    /// changed between preview and confirm (e.g. the excluded live chunk flushed),
    /// so the job deleted nothing and the user must re-select.
    static let reconfirm =
        "The footage changed while you were confirming, so nothing was removed. Select the range again to delete it."

    /// A generic job failure / cancel message.
    static func job(state: String) -> String {
        switch state {
        case "cancelled":
            return "The delete was cancelled. Nothing more was removed."
        default:
            return "Couldn't remove that range. Nothing was removed — try again."
        }
    }

    /// The job kept running but we lost the ability to observe its result (the
    /// status stream stopped responding). It may have removed some or all of the
    /// range, so this must NOT claim "nothing was removed" — the reloaded strip
    /// shows whatever actually happened.
    static let unconfirmed =
        "Couldn't confirm the result — check the day to see what was removed."

    /// Map a thrown `DaemonClientError` to plain copy (sealed store, bad range,
    /// daemon down). Falls back to the underlying description.
    static func fromError(_ error: Error) -> String {
        if case let DaemonClientError.envelopeError(code, _) = error {
            switch code {
            case "store_locked":
                return "Your storage is locked. Unlock it to remove footage. Nothing was removed."
            case "store_absent":
                return "Your storage isn't set up yet, so there's nothing to remove."
            case "invalid_request":
                return "That range isn't valid to delete. Nothing was removed."
            default:
                return "Couldn't remove that range (\(code)). Nothing was removed."
            }
        }
        return "Couldn't remove that range. Nothing was removed. \(error.localizedDescription)"
    }
}

/// U9 (R20, AE2) — the delete confirm sheet: the actual rounded extent, the
/// no-undo + this-Mac-only honesty, and any overlapping clips that will be kept.
/// Confirm/Cancel only; the progress + error states render in the parent while
/// the sheet stays up (`DeleteRangePhase.sheetPresented`).
struct DeleteConfirmSheet: View {
    let content: DeleteConfirmContent
    /// Non-nil while the execute job runs — swaps the buttons for a determinate
    /// progress view (reusing `ReviewWindow`'s `ProgressView(value:)` pattern),
    /// so the sheet morphs from confirm to progress without a flicker.
    let deletingFraction: Double??
    var onConfirm: () -> Void
    var onCancel: () -> Void

    /// `deletingFraction` is a double-optional: `nil` = not deleting (show the
    /// confirm buttons); `.some(nil)` = deleting, indeterminate; `.some(x)` =
    /// deleting at fraction x.
    private var isDeleting: Bool { deletingFraction != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Remove this footage?")
                .font(.title2.weight(.semibold))

            if content.isDeletable {
                Text(content.extentClockText)
                    .font(.headline)
                    .foregroundStyle(.primary)
                Text(chunkSummary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                // A range that resolves to nothing (all gap / only the live chunk):
                // honest, never a false extent or a silent no-op.
                Text("There's no footage in that range to remove.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            // R20 — the two load-bearing honesty statements.
            Label("This can't be undone.", systemImage: "exclamationmark.triangle.fill")
                .font(.callout)
                .foregroundStyle(Color.scRust)
            Label("Removes from this Mac only.", systemImage: "laptopcomputer")
                .font(.callout)
                .foregroundStyle(.secondary)

            if content.hasExcludedLiveChunks {
                // The live in-flight chunk is never deleted — disclose the partial.
                Text("The recording that's still in progress is kept — only footage already saved is removed.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            if !content.keptClips.isEmpty {
                keptClipsDisclosure
            }

            Divider()

            if isDeleting {
                deletingControls
            } else {
                confirmControls
            }
        }
        .padding(24)
        .frame(minWidth: 460, maxWidth: 520)
    }

    /// R20 — overlapping clips are KEPT and stay findable in Clips.
    private var keptClipsDisclosure: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(
                "\(content.keptClips.count) clip\(content.keptClips.count == 1 ? "" : "s") from this range will be kept (find them in Clips):",
                systemImage: "scissors"
            )
            .font(.footnote.weight(.medium))
            .foregroundStyle(.secondary)
            ForEach(content.keptClips) { clip in
                Text("• " + Self.clipRangeText(clip))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var confirmControls: some View {
        HStack {
            Button("Cancel") { onCancel() }
                .keyboardShortcut(.cancelAction)
            Spacer()
            Button("Remove footage", role: .destructive) { onConfirm() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(!content.isDeletable)
        }
    }

    /// Reuses `ReviewWindow`'s determinate `ProgressView(value:)` job pattern —
    /// determinate once the total is known, indeterminate before then.
    private var deletingControls: some View {
        VStack(spacing: 10) {
            if let fraction = deletingFraction, let value = fraction {
                ProgressView(value: value) {
                    Text("Removing footage…").font(.headline)
                }
                .frame(maxWidth: .infinity)
            } else {
                ProgressView("Removing footage…")
                    .controlSize(.small)
            }
        }
    }

    private var chunkSummary: String {
        var parts = ["\(content.totalChunks) segment\(content.totalChunks == 1 ? "" : "s")"]
        if content.recordingCount > 1 {
            parts.append("across \(content.recordingCount) recordings")
        }
        return parts.joined(separator: " ")
    }

    static func clipRangeText(_ clip: DeleteKeptClip) -> String {
        guard let start = clip.startMs, let end = clip.endMs else {
            return clip.sourceDay ?? "clip"
        }
        let range = "\(DeleteConfirmContent.clock(start))–\(DeleteConfirmContent.clock(end))"
        if let day = clip.sourceDay { return "\(day) \(range)" }
        return range
    }
}
