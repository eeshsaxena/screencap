import Foundation

// SCR-182 U4 — recent committed search queries for the Search idle state.
// Backed by UserDefaults so recents survive relaunch; the `defaults` dependency
// is injectable so tests run against an isolated suite instead of polluting
// `.standard` (mirrors `PermissionController`). Recording happens only on an
// explicit commit (Return / chip tap), never on a debounced live run — otherwise
// the list would fill with the typed prefixes of a single intended search.
final class RecentSearchesStore: ObservableObject {
    static let cap = 6
    private static let defaultsKey = "search.recentQueries"

    private let defaults: UserDefaults
    @Published private(set) var recent: [String]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.recent = defaults.stringArray(forKey: Self.defaultsKey) ?? []
    }

    /// Record a committed query: trim, move-to-front (dedupe), cap at `cap`.
    /// Empty/whitespace-only queries are ignored. The new casing wins on a
    /// re-search so the displayed chip matches what the user just typed.
    func record(_ query: String) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        var next = recent.filter { $0.caseInsensitiveCompare(trimmed) != .orderedSame }
        next.insert(trimmed, at: 0)
        if next.count > Self.cap { next = Array(next.prefix(Self.cap)) }
        recent = next
        defaults.set(next, forKey: Self.defaultsKey)
    }
}
