import AppKit
import SwiftUI
import XCTest
@testable import Screencap

// SCR-184 U2 — the SwiftUI view-hosting test utility (born for the retired
// sidebar Search pane's layout guards; the Recall palette state tests build on
// it now). Hosts a view in an NSHostingView inside an offscreen window, forces
// a layout pass, and exposes a rendered-content check over the result.
//
// Why an offscreen window: NSHostingView lays out its SwiftUI content reliably
// only once it is in a window and a layout pass is forced; a detached host can
// report zero sizes. The window is borderless and parked far offscreen so it
// never flashes during a test run.
//
// Measurement strategy (resolved empirically during implementation): SwiftUI's
// accessibility tree is not readable from this unit-test host, and `.accessibility-
// Identifier` does not reliably land on a discrete NSView. The dependable signal
// is therefore a non-blank render check over the raw bitmap.
@MainActor
enum ViewHost {

    static let defaultSize = CGSize(width: 800, height: 600)

    /// A hosted view plus the window that retains it. Keep the handle alive for
    /// the duration of the assertions — releasing the window can tear down the
    /// hosted tree mid-measurement.
    final class Hosted {
        let window: NSWindow
        let root: NSHostingView<AnyView>
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
    /// render pass: a view only rasterizes its content once that pass has run.
    /// The window is NOT ordered front or made key — it stays out of `NSApp`'s
    /// window list, so nothing leaks across the test suite — and is held only by
    /// the returned `Hosted`, so it deallocates with it.
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
        // Pin the light appearance: the design under test is the fixed
        // warm-cream system, and the anti-blank render check reads raw pixels —
        // on a Dark-mode machine the unpinned offscreen window rendered the
        // palette variants as a uniform bitmap and failed every render check
        // (U14, reproduced on main).
        window.appearance = NSAppearance(named: .aqua)
        window.contentView = root

        // Force a full layout pass, then let SwiftUI commit a render so the
        // content rasterizes.
        root.needsLayout = true
        root.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        root.layoutSubtreeIfNeeded()
        return Hosted(window: window, root: root)
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

/// SCR-184 U2 — builders for the view-facing result types so each state test
/// reads as one clear arrange step. These mirror what `SearchViewModel`
/// produces, constructed directly (no daemon, no socket).
@MainActor
enum SearchFixtures {

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

    /// Non-authoritative empty: free-text search with no matches and no time
    /// window → "No matches".
    static func noMatchesResults() -> SearchResults {
        emptyResults(screen: .empty, activity: .empty, queryTerms: ["nonexistent"])
    }

    /// A zero-hit result set with explicit per-stream coverage — the SCR-261
    /// empty-cause derivation is driven entirely by this shape. Defaults are
    /// wire-realistic for a free-text query (SCR-176): transcript searched,
    /// activity `.notRun`.
    static func emptyResults(
        screen: StreamState,
        audio: StreamState = .empty,
        activity: StreamState = .notRun,
        consentNeeded: Bool = false,
        queryTerms: [String] = ["salesforce"]
    ) -> SearchResults {
        SearchResults(
            items: [],
            coverage: CoverageReport(screen: screen, audio: audio, activity: activity),
            consentNeeded: consentNeeded,
            timeWindow: nil,
            appFilter: nil,
            queryTerms: queryTerms
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
