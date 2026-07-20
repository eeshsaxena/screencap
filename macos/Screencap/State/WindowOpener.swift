import Foundation

/// SwiftUI→AppKit bridge that exposes the `openWindow(id:)` action to call
/// sites that live outside the SwiftUI environment chain — primarily
/// `AppDelegate.applicationShouldHandleReopen`, which runs in an AppKit
/// context and cannot read `@Environment(\.openWindow)` directly.
///
/// The closure is registered by `OpenWindowBridge` (a zero-frame helper view
/// inside the main `WindowGroup` body) on `.onAppear` / `.task`. Optional
/// chaining at every call site guards against the bridge firing before the
/// first window has rendered.
@MainActor
final class WindowOpener: ObservableObject {
    static let shared = WindowOpener()

    var openMain: (() -> Void)?

    private init() {}
}
