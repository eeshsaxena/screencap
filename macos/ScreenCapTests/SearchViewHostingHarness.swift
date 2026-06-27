import AppKit
import SwiftUI
import XCTest
@testable import ScreenCap

// SCR-184 U2 — the first SwiftUI view-hosting test utility in this repo. Hosts a
// view in an NSHostingView inside an offscreen window, forces a layout pass, and
// exposes traversal + measurement helpers over the resulting AppKit tree. The
// regression guard (U3) and state coverage (U4) build on this.
//
// Why an offscreen window: NSHostingView lays out its SwiftUI content reliably
// only once it is in a window and a layout pass is forced; a detached host can
// report zero sizes. The window is borderless and parked far offscreen so it
// never flashes during a test run.
//
// Measurement strategy (resolved empirically during implementation): SwiftUI's
// accessibility tree is not readable from this unit-test host, and `.accessibility-
// Identifier` does not reliably land on a discrete NSView. The dependable signals
// are therefore the AppKit backing controls (a `List` backs to `NSScrollView` +
// `NSTableView`; a `ProgressView` to `NSProgressIndicator`) and a non-blank render
// check. Row count from the backing `NSTableView` — not document-view height,
// which fills the viewport regardless of content — is what distinguishes a
// populated results pane from an empty one.
@MainActor
enum SearchViewHost {

    static let defaultSize = CGSize(width: 800, height: 600)

    /// A hosted view plus the window that retains it. Keep the handle alive for
    /// the duration of the assertions — releasing the window can tear down the
    /// hosted tree mid-measurement.
    final class Hosted {
        let window: NSWindow
        let root: NSHostingView<AnyView>
        /// The flattened AppKit subtree, computed once after layout and reused by
        /// every finder (`views(ofType:)`, `resultsScrollView`, `resultsRowCount`)
        /// so a single test doesn't re-walk the whole tree several times.
        lazy var allDescendants: [NSView] = SearchViewHost.descendants(of: root)
        init(window: NSWindow, root: NSHostingView<AnyView>) {
            self.window = window
            self.root = root
        }
    }

    /// Host a SwiftUI view at a fixed size and force layout. Returns a handle the
    /// caller must retain until it has finished measuring.
    ///
    /// The view is hosted in an offscreen window (the window backs SwiftUI's
    /// layout/render path) and the run loop is spun briefly so SwiftUI commits a
    /// render pass: a `List`'s backing table only reports its row count, and a
    /// view only rasterizes its content, once that pass has run. The window is
    /// NOT ordered front or made key — it stays out of `NSApp`'s window list, so
    /// nothing leaks across the test suite — and is held only by the returned
    /// `Hosted`, so it deallocates with it.
    static func host<V: View>(_ view: V, size: CGSize = defaultSize) -> Hosted {
        let root = NSHostingView(rootView: AnyView(view))
        root.frame = CGRect(origin: .zero, size: size)

        let window = NSWindow(
            contentRect: CGRect(origin: .zero, size: size),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        window.contentView = root

        // Force a full layout pass, then let SwiftUI commit a render so the
        // backing table populates and the content rasterizes.
        root.needsLayout = true
        root.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        root.layoutSubtreeIfNeeded()
        return Hosted(window: window, root: root)
    }

    /// Wrap content the way `MainWindow` does: nested below a sibling in a
    /// `VStack` inside a `NavigationSplitView` detail column. This reproduces the
    /// detail-column height-proposal behavior that produced the SCR-174
    /// starvation — a bare fixed frame hands every child a definite height and so
    /// does NOT reproduce the bug. The `Color.clear` sibling stands in for the
    /// `RecordingBanner` that sits above `SearchView` in the real detail.
    static func detailColumn<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        NavigationSplitView {
            Color.clear
        } detail: {
            VStack(spacing: 0) {
                Color.clear.frame(height: 40)
                content()
            }
        }
    }

    // MARK: - NSView traversal

    static func descendants(of root: NSView) -> [NSView] {
        var out: [NSView] = []
        for sub in root.subviews {
            out.append(sub)
            out.append(contentsOf: descendants(of: sub))
        }
        return out
    }

    static func views<T: NSView>(ofType type: T.Type, in hosted: Hosted) -> [T] {
        hosted.allDescendants.compactMap { $0 as? T }
    }

    /// The backing scroll view for the results `List`, if present. The tallest
    /// scroll view is the results region (the only `List` in the Search tree).
    static func resultsScrollView(in hosted: Hosted) -> NSScrollView? {
        views(ofType: NSScrollView.self, in: hosted)
            .max(by: { $0.bounds.height < $1.bounds.height })
    }

    /// The number of rows the results List's backing table reports. This is the
    /// reliable "are results actually rendering content?" signal: a SwiftUI List's
    /// document view fills the viewport regardless of content (so its *height* is
    /// useless here), but the backing `NSTableView`'s `numberOfRows` reflects the
    /// logical row count from the data source — coverage row + section headers +
    /// one row per result. Returns 0 when there is no results List. Falls back to
    /// an `NSCollectionView` item count for SwiftUI list backings that use one.
    static func resultsRowCount(in hosted: Hosted) -> Int {
        let all = hosted.allDescendants
        if let table = all.compactMap({ $0 as? NSTableView }).first {
            return table.numberOfRows
        }
        if let collection = all.compactMap({ $0 as? NSCollectionView }).first {
            return collection.numberOfItems(inSection: 0)
        }
        return 0
    }

    // MARK: - Rendered-content (anti-blank) check

    /// Whether the hosted view actually drew visible content, i.e. its rendered
    /// pixels are not a single uniform color. This is the anti-blank-pane guard —
    /// the SCR-174 bug rendered a *blank* detail — and it works for every state
    /// (message text, progress, List) without depending on the accessibility tree
    /// or on reading SwiftUI text. Not a golden-image comparison: there is no
    /// stored reference and no font/anti-aliasing sensitivity, only "is anything
    /// there?".
    static func rendersVisibleContent(in hosted: Hosted) -> Bool {
        let view = hosted.root
        let bounds = view.bounds
        guard bounds.width > 1, bounds.height > 1,
              let rep = view.bitmapImageRepForCachingDisplay(in: bounds)
        else { return false }
        view.cacheDisplay(in: bounds, to: rep)

        // Compare raw bitmap bytes rather than `colorAt` + colorspace conversion:
        // byte comparison is gamut-agnostic (P3 / any colorspace) and cannot
        // silently skip pixels, so "non-uniform = rendered something" is reliable.
        guard let data = rep.bitmapData else { return false }
        let w = rep.pixelsWide, h = rep.pixelsHigh
        let bytesPerRow = rep.bytesPerRow
        let pixelStride = max(1, rep.bitsPerPixel / 8)
        guard w > 0, h > 0, bytesPerRow > 0 else { return false }

        let stepX = max(1, w / 48), stepY = max(1, h / 48)
        var reference: [UInt8]?
        for y in stride(from: 0, to: h, by: stepY) {
            for x in stride(from: 0, to: w, by: stepX) {
                let offset = y * bytesPerRow + x * pixelStride
                let pixel = (0..<pixelStride).map { data[offset + $0] }
                if let r = reference {
                    if pixel != r { return true }
                } else {
                    reference = pixel
                }
            }
        }
        return false
    }

}

// MARK: - Fixtures

/// SCR-184 U2 — builders for the view-facing result types so each layout/state
/// test reads as one clear arrange step. These mirror what `SearchViewModel`
/// produces, constructed directly (no daemon, no socket).
@MainActor
enum SearchFixtures {

    /// The view under test, wired with inert defaults (no thumbnails, no
    /// selection, no-op callbacks) so each test supplies only the `phase`. Keeps
    /// the 9-parameter construction in one place across the layout + state tests.
    static func resultsView(_ phase: SearchViewModel.Phase) -> SearchResultsView {
        SearchResultsView(
            phase: phase,
            consentDeclined: false,
            selection: .constant(nil),
            frameIndex: nil,
            thumbnailLoader: nil,
            isSearchFieldFocused: false,
            onEnableConsent: {},
            onDeclineConsent: {},
            onOpen: { _ in }
        )
    }

    /// One anchored result on the given day-offset and clock time.
    static func item(
        stream: SearchResultItem.Stream = .activity,
        recording: String = "rec-001",
        daysAgo: Int = 0,
        hour: Int = 14,
        snippet: String? = nil,
        app: String? = "Safari",
        title: String? = "Example tab",
        approximate: Bool = false,
        idSuffix: String = "0"
    ) -> SearchResultItem {
        let anchor = anchorMs(daysAgo: daysAgo, hour: hour)
        return SearchResultItem(
            id: "\(stream.rawValue)-\(recording)-\(anchor)-\(idSuffix)",
            stream: stream,
            recording: recording,
            anchorMs: anchor,
            approximate: approximate,
            score: 0,
            snippet: snippet,
            app: app,
            title: title
        )
    }

    /// An unanchored (audio, time-approximate) result that surfaces off the
    /// per-day timeline.
    static func unanchoredAudioItem(
        recording: String = "rec-002",
        snippet: String = "discussed the salesforce migration",
        idSuffix: String = "u"
    ) -> SearchResultItem {
        SearchResultItem(
            id: "audio-\(recording)-u-\(idSuffix)",
            stream: .audio,
            recording: recording,
            anchorMs: nil,
            approximate: true,
            score: 0,
            snippet: snippet,
            app: nil,
            title: nil
        )
    }

    /// A multi-day loaded result set (today + two days ago), three anchored rows.
    static func multiDayResults() -> SearchResults {
        SearchResults(
            items: [
                item(daysAgo: 0, hour: 15, app: "Safari", title: "Docs", idSuffix: "a"),
                item(daysAgo: 0, hour: 9, app: "Slack", title: "Standup", idSuffix: "b"),
                item(daysAgo: 2, hour: 11, app: "Xcode", title: "Build", idSuffix: "c"),
            ],
            coverage: CoverageReport(screen: .notRun, audio: .notRun, activity: .ok(count: 3)),
            consentNeeded: false,
            timeWindow: nil,
            appFilter: nil,
            queryTerms: []
        )
    }

    /// A single anchored result (one day).
    static func singleResult() -> SearchResults {
        SearchResults(
            items: [item(daysAgo: 0, hour: 12, idSuffix: "only")],
            coverage: CoverageReport(screen: .notRun, audio: .notRun, activity: .ok(count: 1)),
            consentNeeded: false,
            timeWindow: nil,
            appFilter: nil,
            queryTerms: []
        )
    }

    /// Results containing only unanchored audio items (the "Heard in audio"
    /// section, no per-day sections).
    static func unanchoredOnlyResults() -> SearchResults {
        SearchResults(
            items: [unanchoredAudioItem(idSuffix: "a"), unanchoredAudioItem(recording: "rec-003", idSuffix: "b")],
            coverage: CoverageReport(screen: .notRun, audio: .ok(count: 2), activity: .notRun),
            consentNeeded: false,
            timeWindow: nil,
            appFilter: nil,
            queryTerms: ["salesforce"]
        )
    }

    /// Authoritative empty: a time window was given and activity came back empty
    /// → "Nothing recorded then".
    static func authoritativeEmptyResults() -> SearchResults {
        SearchResults(
            items: [],
            coverage: CoverageReport(screen: .notRun, audio: .notRun, activity: .empty),
            consentNeeded: false,
            timeWindow: TimeWindow(startMs: anchorMs(daysAgo: 1, hour: 9), endMs: anchorMs(daysAgo: 1, hour: 17)),
            appFilter: nil,
            queryTerms: []
        )
    }

    /// Non-authoritative empty: free-text search with no matches and no time
    /// window → "No matches".
    static func noMatchesResults() -> SearchResults {
        SearchResults(
            items: [],
            coverage: CoverageReport(screen: .empty, audio: .empty, activity: .empty),
            consentNeeded: false,
            timeWindow: nil,
            appFilter: nil,
            queryTerms: ["nonexistent"]
        )
    }

    /// Loaded results with the consent banner active (free-text, indexing off).
    static func consentNeededResults() -> SearchResults {
        SearchResults(
            items: [item(stream: .activity, daysAgo: 0, hour: 13, idSuffix: "c")],
            coverage: CoverageReport(screen: .notIndexed, audio: .ok(count: 1), activity: .ok(count: 1)),
            consentNeeded: true,
            timeWindow: nil,
            appFilter: nil,
            queryTerms: ["salesforce"]
        )
    }

    /// A fixed epoch-ms anchor derived from a day offset + hour, so fixtures are
    /// deterministic relative to "now" without depending on wall-clock minutes.
    static func anchorMs(daysAgo: Int, hour: Int) -> Int {
        let cal = Calendar.current
        let startOfToday = cal.startOfDay(for: Date())
        let day = cal.date(byAdding: .day, value: -daysAgo, to: startOfToday) ?? startOfToday
        let dated = cal.date(byAdding: .hour, value: hour, to: day) ?? day
        return Int(dated.timeIntervalSince1970 * 1000)
    }
}
