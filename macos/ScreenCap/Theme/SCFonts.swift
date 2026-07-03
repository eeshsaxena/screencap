import AppKit
import CoreText
import Foundation
import SwiftUI

/// Registration + PostScript-name vocabulary for the four bundled typefaces
/// (Newsreader, Space Grotesk, IBM Plex Mono, Public Sans — all OFL), shipped in
/// `Resources/Fonts/` and declared via `ATSApplicationFontsPath: Fonts`.
///
/// The prototype (KTD-3) supersedes SCR-197's system-font-only decision. macOS
/// processes `ATSApplicationFontsPath` at launch, but that path is app-launch
/// machinery — a unit-test host does not run it — so `SCFonts.register()` also
/// registers the files programmatically. Calling it at app startup is
/// belt-and-suspenders (a folder-reference packaging slip can't silently drop the
/// design's type); calling it from tests makes the "loads by PostScript name"
/// contract assertable in-process.
///
/// The static instances are pre-baked at build time (see the plan's U1 notes):
/// three of the four upstream families ship as variable fonts whose *default*
/// instance is their thinnest weight (Space Grotesk → Light, Public Sans → Thin)
/// with messy PostScript names — so the bundle carries clean, single-weight
/// instances with the predictable PostScript names below instead.
enum SCFonts {

    /// PostScript names of every bundled instance — the exact strings
    /// `NSFont(name:)` / `Font.custom(_:size:)` resolve against.
    enum Newsreader {
        static let regular = "Newsreader-Regular"
        static let medium = "Newsreader-Medium"
        static let italic = "Newsreader-Italic"
    }
    enum SpaceGrotesk {
        static let regular = "SpaceGrotesk-Regular"
        static let medium = "SpaceGrotesk-Medium"
        static let semibold = "SpaceGrotesk-SemiBold"
        static let bold = "SpaceGrotesk-Bold"
    }
    enum IBMPlexMono {
        static let regular = "IBMPlexMono-Regular"
        static let medium = "IBMPlexMono-Medium"
    }
    enum PublicSans {
        static let regular = "PublicSans-Regular"
        static let medium = "PublicSans-Medium"
        static let semibold = "PublicSans-SemiBold"
        static let bold = "PublicSans-Bold"
    }

    /// One representative PostScript name per family — the "does the family load?"
    /// probe the token tests assert against.
    static let familyProbeNames = [
        Newsreader.regular, SpaceGrotesk.regular, IBMPlexMono.regular, PublicSans.regular,
    ]

    // Single-threaded by construction: written only by `registerBundledFonts()`,
    // whose callers (app launch in `ScreenCapApp.init`, test `setUp`) run on one
    // thread. `nonisolated(unsafe)` states that contract explicitly so the
    // strict-concurrency checker (SWIFT_STRICT_CONCURRENCY: complete) doesn't flag
    // it as shared mutable global state.
    nonisolated(unsafe) private static var didRegisterBundle = false

    /// Register the fonts bundled in the app (idempotent). Safe to call from
    /// `ScreenCapApp` init; a no-op after the first successful call.
    static func registerBundledFonts() {
        guard !didRegisterBundle else { return }
        didRegisterBundle = true
        // `Color.scBundle` is the bundle that owns the design tokens (the app under
        // the app runtime; the host app under the test runner). Look inside the
        // `Fonts/` folder reference first, then fall back to a flat sweep in case
        // packaging copied the files to `Contents/Resources/` directly.
        let bundle = Color.scBundle
        var urls = bundle.urls(forResourcesWithExtension: "ttf", subdirectory: "Fonts") ?? []
        if urls.isEmpty {
            urls = bundle.urls(forResourcesWithExtension: "ttf", subdirectory: nil) ?? []
        }
        register(urls)
    }

    /// Register every `.ttf` under `directory` (used by tests to register straight
    /// from `Resources/Fonts/` in the source tree, independent of app packaging).
    static func register(fontsIn directory: URL) {
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil
        )) ?? []
        register(urls.filter { $0.pathExtension.lowercased() == "ttf" })
    }

    /// Register a set of font-file URLs, tolerating "already registered" so the
    /// programmatic path and `ATSApplicationFontsPath` can both fire without noise.
    private static func register(_ urls: [URL]) {
        for url in urls {
            var error: Unmanaged<CFError>?
            let ok = CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error)
            if !ok, let error = error?.takeRetainedValue() {
                let code = CFErrorGetCode(error)
                // kCTFontManagerErrorAlreadyRegistered (105) / duplicate (305) are benign.
                if code != 105 && code != 305 {
                    NSLog("SCFonts: failed to register \(url.lastPathComponent): \(error)")
                }
            }
        }
    }
}
