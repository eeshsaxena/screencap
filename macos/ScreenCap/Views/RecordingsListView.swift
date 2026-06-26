import SwiftUI

/// Date-grouped recordings list (Unit 12). Default state: all recordings,
/// newest day first. When `filterDay` is set, shows only recordings from that
/// day; "Show all" breadcrumb clears the filter.
///
/// Row click opens the native read-only inspect window for the recording
/// ("just looking"), replacing the old `screencap view` browser link-out. The
/// per-row Upload button still opens the upload/consent window; the CLI
/// `screencap view` HTML viewer is unchanged for power users.
struct RecordingsListView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @Environment(\.openWindow) private var openWindow

    @Binding var filterDay: Date?
    /// Lets the parent navigate back to the calendar when the user clicks a
    /// date header (per origin's Visual Aid section nav contract).
    var onSelectDayHeader: (Date) -> Void

    @State private var rowError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
        }
        .alert("Failed to open recording", isPresented: errorBinding) {
            Button("OK") { rowError = nil }
        } message: {
            Text(rowError ?? "")
        }
    }

    private var header: some View {
        HStack {
            if let day = filterDay {
                Button {
                    filterDay = nil
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.left")
                        Text("Show all")
                    }
                }
                .buttonStyle(.borderless)

                Text("·")
                    .foregroundStyle(.secondary)

                Text(longFormat(day))
                    .font(.headline)
            } else {
                Text("All Recordings")
                    .font(.headline)
            }
            Spacer()
            Text("\(visibleRecordings.count)")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    @ViewBuilder
    private var content: some View {
        if let day = filterDay, visibleRecordings.isEmpty {
            VStack(spacing: 8) {
                Text("No recordings on \(longFormat(day))")
                    .font(.body)
                    .foregroundStyle(.secondary)
                Button("Show all recordings") { filterDay = nil }
                    .buttonStyle(.borderless)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(40)
        } else if visibleRecordings.isEmpty {
            VStack(spacing: 8) {
                Text("No recordings yet")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(40)
        } else {
            List {
                if filterDay == nil {
                    ForEach(index.groupedByDay(), id: \.day) { group in
                        Section {
                            ForEach(group.recordings) { rec in
                                row(for: rec)
                            }
                        } header: {
                            Button {
                                onSelectDayHeader(group.day)
                            } label: {
                                HStack {
                                    Text(longFormat(group.day))
                                        .font(.subheadline.bold())
                                    Image(systemName: "chevron.right")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .buttonStyle(.plain)
                        }
                    }
                } else {
                    ForEach(visibleRecordings) { rec in
                        row(for: rec)
                    }
                }
            }
            .listStyle(.inset)
        }
    }

    @ViewBuilder
    private func row(for rec: RecordingSummary) -> some View {
        Button {
            openInspect(rec)
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(rec.name)
                        .font(.body)
                    HStack(spacing: 8) {
                        Text(rec.startedTimeOfDay)
                        Text("·")
                        Text(rec.duration)
                        if rec.hasAudio {
                            Text("·")
                            Image(systemName: "waveform")
                        }
                        // `is_stub` means "uploaded; local media files have
                        // been deleted" (see catalog.py). It is NOT the
                        // force-stopped / incomplete-recording state that
                        // DL-005 asks for — that signal will live on a
                        // separate field once Unit 13 emits it. Until then
                        // we surface no per-row indicator for incomplete
                        // recordings rather than mislabel uploaded ones.
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                Text(rec.sizeMB)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                if rec.isUploadEligible {
                    uploadButton(for: rec)
                }
                Image(systemName: "play.circle")
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// Plan U4: per-row Upload affordance. Visible only for eligible rows
    /// (`uploaded == false && isStub == false`, per R2). Opens the review
    /// `WindowGroup` (plan U3) scoped to this recording — the review-then-
    /// upload pipeline lives in U7/U8. The button has its own `.buttonStyle`
    /// scope so its tap area doesn't fight the outer row button (which now
    /// opens the read-only inspect window on row body click).
    @ViewBuilder
    private func uploadButton(for rec: RecordingSummary) -> some View {
        Button {
            openWindow(id: ReviewWindowID, value: rec.name)
        } label: {
            Label("Upload", systemImage: "icloud.and.arrow.up")
                .labelStyle(.titleAndIcon)
                .font(.caption)
        }
        .buttonStyle(.borderless)
        .help("Review this recording before uploading")
    }

    private var errorBinding: Binding<Bool> {
        Binding(
            get: { rowError != nil },
            set: { if !$0 { rowError = nil } }
        )
    }

    private var visibleRecordings: [RecordingSummary] {
        if let day = filterDay {
            return index.recordings(on: day)
        }
        return index.recordings.sorted(by: RecordingSummary.newestFirst)
    }

    private static let longDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .full
        return f
    }()

    private func longFormat(_ day: Date) -> String {
        Self.longDateFormatter.string(from: day)
    }

    /// Row click opens the native read-only inspect window for the recording
    /// ("just looking"), replacing the old `screencap view` browser link-out. A
    /// stub recording (`is_stub == true`) has had its local media deleted after
    /// upload, so there is nothing local to inspect — surface the friendly
    /// download message instead (shared with the search entry point via
    /// `InspectRouting`). Opens at the recording's start (no seek). The per-row
    /// Upload button is unchanged — it still opens the upload/consent window.
    private func openInspect(_ rec: RecordingSummary) {
        switch InspectRouting.decide(recording: rec.name, anchorMs: nil, isStub: rec.isStub) {
        case .unavailable(let message):
            rowError = message
        case .open(let recording, _):
            // Recordings open at the start — clear any stale pending seek so a
            // reused inspect window doesn't jump to a prior search moment.
            InspectWindowOpener.shared.pendingSeekMs[recording] = nil
            openWindow(id: InspectWindowID, value: recording)
        }
    }
}
