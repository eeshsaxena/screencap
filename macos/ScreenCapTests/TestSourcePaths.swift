import Foundation

/// Shared helper for the SCR-197 source-level guard tests (the launch-surface
/// grep guard and the color-set JSON assertions), which read macOS app *source*
/// files rather than runtime artifacts. Both anchor on the test file's own
/// `#filePath` and walk up to the `macos/` directory, so the "where do the
/// sources live relative to a test" computation lives in exactly one place.
enum TestSourcePaths {

    /// Resolve `relativePath` against the `macos/` directory (the parent of
    /// `ScreenCapTests/`), derived from the calling test file's location.
    /// `filePath` defaults to the call site via `#filePath`.
    static func macosFile(
        _ relativePath: String,
        from filePath: StaticString = #filePath
    ) -> URL {
        URL(fileURLWithPath: "\(filePath)")
            .deletingLastPathComponent()   // ScreenCapTests/
            .deletingLastPathComponent()   // macos/
            .appendingPathComponent(relativePath)
    }
}
