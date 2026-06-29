import SwiftUI

// SCR-181 U2 — the scrubbable per-day search timeline (closes the R5 gap: v1
// shipped per-day List sections, R5 wanted positioned markers on a time axis).
// A horizontal day-strip navigator selects a day; below it a Canvas draws the
// day's hits as markers on a 24h axis (raw ticks batched one Path per stream —
// the TimelinePane density pattern), with bounded, interactive node overlays on
// top (clustering via SearchTimelineLayout keeps the overlay count small on a
// busy day, R5a). Tapping a single marker opens inspect at the moment (the
// already-wired onOpen path); tapping a cluster discloses its members inline.
//
// This view pins ABOVE the results List via `.safeAreaInset` in SearchResultsView
// (U3) — it never becomes a VStack sibling of the List (the SCR-174 starvation).
struct SearchDayTimeline: View {
    /// Day buckets (most-recent first) from `searchResultsGroupedByDay`; only
    /// anchored items reach here (unanchored audio stays in the companion list).
    let days: [(day: Date, items: [SearchResultItem])]
    /// The day whose axis is shown. Owned by the parent so it can reset on a new
    /// search; defaults to the most-recent day with a hit when nil.
    @Binding var selectedDay: Date?
    /// Shared with the companion list so a marker and a row highlight together and
    /// the existing Return-to-open handler acts on a marker selection (U4).
    @Binding var selection: SearchResultItem.ID?
    let onOpen: (SearchResultItem) -> Void

    /// Markers within this many points chain into one cluster (SearchTimelineLayout).
    /// A generous, hit-friendly default; tunable in one place.
    var clusterThresholdPx: CGFloat = 14

    private let axisHeight: CGFloat = 52
    private let calendar = Calendar.current

    // Marker dimensions, named so the dot, its selection ring (dot + 4), and the
    // hit target stay in sync from one place (the ring/target sizes derive from
    // these rather than repeating magic numbers).
    private let singleDotSize: CGFloat = 9
    private let clusterDotSize: CGFloat = 18
    private let hitTargetWidth: CGFloat = 22

    /// The cluster currently disclosed inline (its members listed below the axis).
    @State private var disclosed: [SearchResultItem]?

    private var activeDay: Date? { selectedDay ?? days.first?.day }

    private var activeItems: [SearchResultItem] {
        guard let activeDay else { return [] }
        return days.first { calendar.isDate($0.day, inSameDayAs: activeDay) }?.items ?? []
    }

    /// `[startMs, endMs)` for the active day — local midnight + 24h (uniform across
    /// days; matches `SearchTimelineLayout.dayBounds`).
    private var activeBounds: (startMs: Int, endMs: Int) {
        guard let activeDay else { return (0, 86_400_000) }
        let startMs = Int(calendar.startOfDay(for: activeDay).timeIntervalSince1970 * 1000)
        return (startMs, startMs + 86_400_000)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if days.count > 1 { dayStrip }
            axis
            if let disclosed { clusterDisclosure(disclosed) }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    // MARK: - Day strip

    private var dayStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(days, id: \.day) { group in
                    dayChip(group.day, count: group.items.count)
                }
            }
            .padding(.horizontal, 2)
        }
    }

    /// A tappable day chip. A plain tappable element (NOT a `Button`) so it never
    /// claims the window default action — the SCR-183 discipline applied here.
    private func dayChip(_ day: Date, count: Int) -> some View {
        let isActive = activeDay.map { calendar.isDate($0, inSameDayAs: day) } ?? false
        return Text(searchDayLabel(day))
            .font(.caption)
            .fontWeight(isActive ? .semibold : .regular)
            .padding(.vertical, 4)
            .padding(.horizontal, 10)
            .background(
                isActive ? Color.accentColor.opacity(0.18) : Color(nsColor: .controlBackgroundColor),
                in: Capsule()
            )
            .overlay(Capsule().strokeBorder(isActive ? Color.accentColor : Color(nsColor: .separatorColor)))
            .contentShape(Capsule())
            .onTapGesture {
                selectedDay = day
                disclosed = nil
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(searchDayLabel(day)), \(count) result\(count == 1 ? "" : "s")")
            .accessibilityAddTraits(isActive ? [.isButton, .isSelected] : .isButton)
    }

    // MARK: - Axis

    private var axis: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let (startMs, endMs) = activeBounds
            let markers = SearchTimelineLayout.placeMarkers(
                activeItems, startMs: startMs, endMs: endMs, width: width
            )
            let nodes = SearchTimelineLayout.cluster(markers, thresholdPx: clusterThresholdPx)

            ZStack(alignment: .topLeading) {
                Canvas { context, size in
                    drawBaselineAndGrid(context, size)
                    drawMarkers(context, size, markers)
                }
                ForEach(nodes, id: \.representative.id) { node in
                    nodeOverlay(node, height: geo.size.height)
                }
            }
        }
        .frame(height: axisHeight)
    }

    /// Baseline + faint vertical gridlines every 6 hours (00/06/12/18/24) so the
    /// axis reads as a clock without crowding it with 24 lines.
    private func drawBaselineAndGrid(_ context: GraphicsContext, _ size: CGSize) {
        let baselineY = size.height / 2
        var baseline = Path()
        baseline.move(to: CGPoint(x: 0, y: baselineY))
        baseline.addLine(to: CGPoint(x: size.width, y: baselineY))
        context.stroke(baseline, with: .color(.gray.opacity(0.25)), lineWidth: 1)

        var grid = Path()
        for hour in stride(from: 0, through: 24, by: 6) {
            let x = size.width * CGFloat(hour) / 24
            grid.move(to: CGPoint(x: x, y: 0))
            grid.addLine(to: CGPoint(x: x, y: size.height))
        }
        context.stroke(grid, with: .color(.gray.opacity(0.12)), lineWidth: 1)
    }

    /// Raw markers as thin ticks, batched into one Path per stream (the
    /// TimelinePane density pattern: at most one stroke per stream, not per
    /// marker). Purely visual — interaction lives in the bounded node overlays.
    private func drawMarkers(_ context: GraphicsContext, _ size: CGSize, _ markers: [SearchTimelineLayout.Marker]) {
        let baselineY = size.height / 2
        let half: CGFloat = 9
        var pathsByStream: [SearchResultItem.Stream: Path] = [:]
        for marker in markers {
            let x = marker.x
            pathsByStream[marker.item.stream, default: Path()].move(to: CGPoint(x: x, y: baselineY - half))
            pathsByStream[marker.item.stream]?.addLine(to: CGPoint(x: x, y: baselineY + half))
        }
        for (stream, path) in pathsByStream {
            context.stroke(path, with: .color(stream.tint.opacity(0.6)), lineWidth: 1.5)
        }
    }

    // MARK: - Node overlays (interaction)

    /// One bounded, tappable mark per clustered node, positioned at the node's x.
    /// A single is a stream-tinted dot; a cluster is an accent dot with its count.
    /// The selected node (its members contain the shared `selection`) gets a ring.
    /// A plain tap target (NOT a `Button`) so it never claims the window default
    /// action (SCR-183). The accessibility label is applied in U4.
    private func nodeOverlay(_ node: TimelineNode, height: CGFloat) -> some View {
        let isSelected = selection.map { id in node.items.contains { $0.id == id } } ?? false
        let ringSize = (node.isCluster ? clusterDotSize : singleDotSize) + 4
        return nodeMark(node)
            .overlay {
                if isSelected {
                    Circle().strokeBorder(Color.primary, lineWidth: 2)
                        .frame(width: ringSize, height: ringSize)
                }
            }
            .frame(width: hitTargetWidth, height: height)
            .contentShape(Rectangle())
            .onTapGesture { tap(node) }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(SearchAccessibility.timelineNodeLabel(node))
            .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : .isButton)
            .position(x: node.x, y: height / 2)
    }

    @ViewBuilder
    private func nodeMark(_ node: TimelineNode) -> some View {
        if node.isCluster {
            ZStack {
                Circle().fill(Color.accentColor)
                Text("\(node.count)")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.white)
            }
            .frame(width: clusterDotSize, height: clusterDotSize)
        } else {
            Circle()
                .fill(node.representative.stream.tint)
                .frame(width: singleDotSize, height: singleDotSize)
        }
    }

    /// A single marker opens inspect at its moment (the wired onOpen path); a
    /// cluster discloses its members inline so each is reachable (never auto-opens
    /// an arbitrary one). Either way the shared selection updates for keyboard /
    /// companion-row parity.
    private func tap(_ node: TimelineNode) {
        selection = node.representative.id
        if node.isCluster {
            disclosed = node.items
        } else {
            disclosed = nil
            onOpen(node.representative)
        }
    }

    // MARK: - Cluster disclosure

    private func clusterDisclosure(_ items: [SearchResultItem]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("\(items.count) results in this moment")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.secondary)
                    .contentShape(Rectangle())
                    .onTapGesture { disclosed = nil }
                    .accessibilityLabel("Close")
                    .accessibilityAddTraits(.isButton)
            }
            .padding(.bottom, 4)
            ScrollView {
                VStack(spacing: 0) {
                    ForEach(items) { item in
                        clusterMemberRow(item)
                    }
                }
            }
            .frame(maxHeight: 160)
        }
        .padding(8)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }

    /// A compact disclosed-cluster member. Plain tap target (SCR-183); opens the
    /// member and dismisses the disclosure.
    private func clusterMemberRow(_ item: SearchResultItem) -> some View {
        HStack(spacing: 8) {
            Image(systemName: item.streamIcon)
                .foregroundStyle(item.streamTint)
                .frame(width: 16)
                .accessibilityHidden(true)
            Text(item.primaryText)
                .font(.caption)
                .lineLimit(1)
            Spacer(minLength: 8)
            Text(item.timeLabel)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 5)
        .contentShape(Rectangle())
        .onTapGesture {
            selection = item.id
            disclosed = nil
            onOpen(item)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(SearchAccessibility.resultRowLabel(item))
        .accessibilityAddTraits(.isButton)
    }
}
