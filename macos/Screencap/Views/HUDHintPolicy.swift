/// One-time menu-bar hint — pure gating for whether the first-hide hint shows
/// (U5, R7).
///
/// The hint points to the menu bar the first time the user ever hides the
/// recording toolbar, then never again. Factored out of `RecorderController` so
/// the once-only decision is a unit test rather than a manual check, matching the
/// app's `*Policy` convention. The persisted flag lives in `HUDHintStore`; this
/// predicate only decides show-vs-not given that flag.
enum HUDHintPolicy {
    /// Show the hint only if it has never been shown before.
    static func shouldShow(hasShownBefore: Bool) -> Bool {
        !hasShownBefore
    }
}
