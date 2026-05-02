import SwiftUI

/// Date-grouped recordings list (Unit 12). Default state: all recordings,
/// newest day first. When `filterDay` is set, shows only recordings from that
/// day; "Show all" breadcrumb clears the filter.
///
/// Row click triggers Unit 14a (`screencap view <name>` link-out to the user's
/// default browser) — replaced by the native viewer in v1.1.
struct RecordingsListView: View {
    @EnvironmentObject private var index: RecordingsIndex

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
            openInBrowser(rec)
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
                Image(systemName: "play.circle")
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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

    /// Unit 14a: shell out to `screencap view <name>`. The Python side
    /// regenerates `viewer.html` if needed and `open`s it in the default
    /// browser. The `--` separator forces Click to treat the recording
    /// name as a positional argument even if it begins with `--`.
    ///
    /// Uses `runAwaitingExit` (not `runDetached`) so a non-zero exit
    /// surfaces in the row alert instead of looking like a broken click.
    /// Stub recordings (`is_stub == true`) are pre-checked: their local
    /// media has been deleted after upload, so `screencap view` would
    /// fail anyway — surface the friendly explanation instead.
    private func openInBrowser(_ rec: RecordingSummary) {
        if rec.isStub {
            rowError = "This recording was uploaded and the local copy was deleted. Run `screencap download \(rec.name)` to retrieve it."
            return
        }
        Task {
            do {
                try await CLIClient.runAwaitingExit(["view", "--", rec.name], timeout: 15)
            } catch {
                await MainActor.run { rowError = error.localizedDescription }
            }
        }
    }
}
