import SwiftUI

/// Pure presentation logic for `RecordedSummaryPane`, extracted so the
/// loading-vs-empty-vs-digest decision (KTD7) and the blocked-line copy are
/// unit-testable without a SwiftUI render — mirroring `InspectShareAffordance`
/// and `EventContent`.
enum RecordedSummaryDisplay {
    /// Which body the pane renders once expanded.
    enum Mode: Equatable {
        /// The event array is still parsing — the digest is not yet trustworthy.
        case loading
        /// Parsing finished with zero meaningful events (idle / fully blocked).
        case empty
        /// A real digest to show.
        case digest
    }

    /// Distinguish "still parsing" from "genuinely empty" (KTD7): never show the
    /// honest empty state while the detached parse is still in flight, or a long
    /// recording would flash "nothing recorded" before its events decode.
    static func mode(summary: RecordedSummary, isParsing: Bool) -> Mode {
        if isParsing { return .loading }
        if summary.isEmpty { return .empty }
        return .digest
    }

    /// "N blocked intervals" / "1 blocked interval".
    static func blockedCountLabel(_ count: Int) -> String {
        count == 1 ? "1 blocked interval" : "\(count) blocked intervals"
    }

    /// A not-captured span rendered as `Xm Ys` (or `Ys` under a minute) from a
    /// millisecond total.
    static func spanLabel(ms: Int) -> String {
        let totalSeconds = max(0, ms) / 1000
        let minutes = totalSeconds / 60
        let seconds = totalSeconds % 60
        return minutes > 0 ? "\(minutes)m \(seconds)s" : "\(seconds)s"
    }

    /// The blocked reassurance line, e.g. "2 blocked intervals · 1m 5s not captured".
    static func blockedLine(count: Int, spanMs: Int) -> String {
        "\(blockedCountLabel(count)) · \(spanLabel(ms: spanMs)) not captured"
    }

    /// A single blocked range as `HH:MM–HH:MM`, reusing the day strip's wall-clock
    /// formatter so the viewer's ranges match the day timeline (Open Question:
    /// keep blocked labels consistent with `DayStripAccessibility`).
    static func rangeLabel(_ interval: CapturedInterval) -> String {
        "\(DayStripAccessibility.hourMinuteText(ms: interval.startMs))"
            + "–\(DayStripAccessibility.hourMinuteText(ms: interval.endMs))"
    }

    /// VoiceOver label for a blocked range, mirroring `DayStripAccessibility`'s
    /// "Blocked, nothing captured, HH:MM to HH:MM" wording (KTD6).
    static func rangeAccessibilityLabel(_ interval: CapturedInterval) -> String {
        "Blocked, nothing captured, "
            + "\(DayStripAccessibility.hourMinuteText(ms: interval.startMs)) to "
            + "\(DayStripAccessibility.hourMinuteText(ms: interval.endMs))"
    }
}

/// The collapsed-by-default "What was recorded" summary inside the inspect window
/// (R1/R2). Read-only (R8): it renders the `RecordedSummary` digest — apps seen,
/// meaningful-event counts by kind, and a blocked-interval reassurance line — with
/// each app group expandable to its underlying meaningful events (R6). It exposes
/// no affordance to edit, delete, or redact.
///
/// Loading and empty are distinct (KTD7): while the detached event parse is in
/// flight it shows a neutral placeholder; only once parsing completes with zero
/// meaningful events does it show the honest "nothing recorded" state (R7).
struct RecordedSummaryPane: View {
    let summary: RecordedSummary
    /// True while the detached event parse is still running. Drives the
    /// loading-vs-empty distinction so a long recording never flashes an empty
    /// state before its events decode.
    let isParsing: Bool

    /// Collapsed on every open (R2) — no persisted expanded state (deferred).
    @State private var expanded = false

    private var mode: RecordedSummaryDisplay.Mode {
        RecordedSummaryDisplay.mode(summary: summary, isParsing: isParsing)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            body(for: mode)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 8)
        } label: {
            headerLabel
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var headerLabel: some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.shield")
                .foregroundStyle(.secondary)
            Text("What was recorded")
                .font(.callout.weight(.semibold))
            Spacer()
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("What was recorded")
        .accessibilityHint("Shows a summary of the events this recording captured")
    }

    @ViewBuilder
    private func body(for mode: RecordedSummaryDisplay.Mode) -> some View {
        switch mode {
        case .loading:
            loadingState
        case .empty:
            emptyState
        case .digest:
            ScrollView {
                digest.padding(.trailing, 4)
            }
            .frame(maxHeight: 240)
        }
    }

    // A brief neutral placeholder — the digest is still being built (KTD7). Not
    // an error and not the empty state.
    private var loadingState: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("Reading captured events…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Reading captured events")
    }

    // The honest empty state (R7 / AE5). A fully-blocked recording still surfaces
    // its blocked reassurance beneath the plain statement.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Nothing meaningful was recorded here.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if summary.hasBlocked {
                blockedSection
            }
        }
    }

    private var digest: some View {
        VStack(alignment: .leading, spacing: 10) {
            digestHeadline
            ForEach(summary.apps) { app in
                appGroup(app)
            }
            if summary.hasBlocked {
                Divider()
                blockedSection
            }
        }
    }

    private var digestHeadline: some View {
        let appCount = summary.apps.count
        let appWord = appCount == 1 ? "app" : "apps"
        let eventWord = summary.totalMeaningfulCount == 1 ? "event" : "events"
        let kinds = summary.kindCounts.map { "\($0.label) \($0.count)" }
            .joined(separator: " · ")
        return VStack(alignment: .leading, spacing: 2) {
            Text("\(appCount) \(appWord) · \(summary.totalMeaningfulCount) \(eventWord)")
                .font(.callout.weight(.semibold))
            if !kinds.isEmpty {
                Text(kinds)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            "\(appCount) \(appWord), \(summary.totalMeaningfulCount) meaningful \(eventWord) captured"
        )
    }

    /// One expandable app group: label shows the app and its total; expanding
    /// reveals the per-kind breakdown and the underlying meaningful events (R6),
    /// reusing the read-only `EventContentRow` style.
    private func appGroup(_ app: RecordedAppSummary) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 4) {
                if !app.kindCounts.isEmpty {
                    Text(app.kindCounts.map { "\($0.count) \($0.label.lowercased())" }
                        .joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                ForEach(Array(app.events.enumerated()), id: \.offset) { _, event in
                    EventContentRow(event: event)
                    Divider()
                }
            }
            .padding(.top, 2)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: app.isUnknown ? "questionmark.app.dashed" : "macwindow")
                    .foregroundStyle(.secondary)
                Text(app.appName)
                    .font(.callout)
                Spacer()
                Text("\(app.totalCount)")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
            .accessibilityElement(children: .combine)
            .accessibilityLabel(
                "\(app.appName), \(app.totalCount) meaningful "
                    + "\(app.totalCount == 1 ? "event" : "events")"
            )
        }
    }

    /// The blocked reassurance line (KTD5): count + total not-captured span,
    /// expandable to the individual `HH:MM–HH:MM` ranges.
    private var blockedSection: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 4) {
                // Index-keyed: robust even if two intervals ever shared a startMs
                // (the upstream merge makes starts distinct today, but the id
                // shouldn't silently depend on that invariant).
                ForEach(Array(summary.blockedIntervals.enumerated()), id: \.offset) { _, interval in
                    Text(RecordedSummaryDisplay.rangeLabel(interval))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityLabel(
                            RecordedSummaryDisplay.rangeAccessibilityLabel(interval)
                        )
                }
            }
            .padding(.top, 2)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "eye.slash")
                    .foregroundStyle(.secondary)
                Text(RecordedSummaryDisplay.blockedLine(
                    count: summary.blockedCount, spanMs: summary.blockedTotalMs
                ))
                .font(.callout)
                Spacer()
            }
            .contentShape(Rectangle())
            .accessibilityElement(children: .combine)
            .accessibilityLabel(
                "Blocked, nothing captured, "
                    + "\(RecordedSummaryDisplay.blockedCountLabel(summary.blockedCount)), "
                    + "\(RecordedSummaryDisplay.spanLabel(ms: summary.blockedTotalMs)) not captured"
            )
        }
    }
}
