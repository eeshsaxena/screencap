import SwiftUI

/// Moment-selection logic for the content view, extracted so it's unit-testable
/// without a SwiftUI render.
enum EventContent {
    /// The content-bearing events at the current moment — those within
    /// `window` seconds of `time`. Pure mouse/screen events (no content) are
    /// excluded so the view shows captured *content*, not pointer noise.
    static func moment(
        at time: Double, in events: [TimelineEvent], window: Double = 2.0
    ) -> [TimelineEvent] {
        events.filter { !$0.content.isEmpty && abs($0.relativeSeconds - time) <= window }
    }
}

/// Shows the captured event/text content for the current timeline moment, read
/// from the *scrubbed* copy (R5/R6/R7). Redacted fields render as `[redacted]`
/// (the value is never shown); a fail-closed field routes to an inline R14
/// marker rather than masquerading as content. Selecting a row seeks the visual.
struct EventContentPane: View {
    let events: [TimelineEvent]
    let currentTime: Double
    let onSeek: (Double) -> Void

    private var moment: [TimelineEvent] {
        EventContent.moment(at: currentTime, in: events)
    }

    var body: some View {
        // Compute once per render: `moment` is an O(N) scan over events and the
        // body reads it twice (empty check + ForEach) at ~10 Hz playback.
        let moment = self.moment
        return VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if moment.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(moment.enumerated()), id: \.offset) { _, event in
                            EventContentRow(event: event)
                                .contentShape(Rectangle())
                                .onTapGesture { onSeek(event.relativeSeconds) }
                            Divider()
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "text.alignleft")
                .foregroundStyle(.secondary)
            Text("Captured at this moment")
                .font(.caption.weight(.semibold))
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }

    private var emptyState: some View {
        Text("No captured content at this moment.")
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            .padding()
    }
}

/// One event row in the content pane. Renders only the fields that apply to the
/// event, classifying each for honest display.
struct EventContentRow: View {
    let event: TimelineEvent

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                Text(ScreenshotTruth.clockLabel(event.relativeSeconds))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                contentLines
                if event.content.hasFailClosed {
                    failClosedLine
                }
            }
            Spacer()
        }
        .padding(.horizontal, 10)
    }

    @ViewBuilder
    private var contentLines: some View {
        line("App", event.content.appName)
        line("Window", event.content.windowTitle)
        line("Site", event.content.domain)
        line("Typed", event.content.typedText)
        line("Said", event.content.transcription)
        line("Host", event.content.networkHost)
        line("URL", event.content.networkURL)
    }

    /// Render one content field, or nothing when it is absent / fail-closed
    /// (fail-closed is surfaced once via `failClosedLine`, not per field).
    @ViewBuilder
    private func line(_ label: String, _ field: RedactableField) -> some View {
        switch field {
        case .absent, .failClosed:
            EmptyView()
        case .value(let text):
            labeled(label, Text(text).font(.caption))
        case .redacted:
            // `.foregroundColor` (not `.foregroundStyle`) here: this value is
            // typed as `Text` for `labeled`, and `Text.foregroundStyle` is
            // macOS 14+ while `Text.foregroundColor` is available on our 13.0
            // deployment target.
            labeled(label, Text("[redacted]").font(.caption).italic().foregroundColor(.secondary))
        }
    }

    private func labeled(_ label: String, _ value: Text) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 48, alignment: .trailing)
            value.textSelection(.enabled)
        }
    }

    private var failClosedLine: some View {
        HStack(spacing: 4) {
            Image(systemName: "exclamationmark.shield.fill")
                .font(.caption2)
                .foregroundStyle(.orange)
            Text("Couldn't analyze — removed to be safe")
                .font(.caption2)
                .foregroundStyle(.orange)
        }
    }

    private var icon: String {
        switch event.category {
        case .key:    return "keyboard"
        case .window: return "macwindow"
        case .mouse:  return "cursorarrow"
        case .screen: return "photo"
        case .other:  return "waveform"
        }
    }
}
