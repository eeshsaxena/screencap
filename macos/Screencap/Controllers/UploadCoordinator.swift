import Foundation

/// Tracks how many review windows are mid-upload so Sign Out can be disabled
/// while any `request_signed_urls` is in flight (design-review state c — avoids
/// a `NotSignedIn` mid-upload). Split out of `CloudAuthController` so the auth
/// controller owns only sign-in flow + persistent status; upload bookkeeping is
/// a separate, app-wide observable shared the same way (a `@StateObject` in
/// `ScreencapApp` handed to each window via `.environmentObject`).
///
/// Counting (rather than a bool) is deliberate: R3 allows multiple concurrent
/// review windows, so two simultaneous uploads must both have to finish before
/// Sign Out re-enables.
@MainActor
final class UploadCoordinator: ObservableObject {
    @Published private(set) var activeUploadCount = 0

    /// True while at least one review window's upload is in flight.
    var isUploadInFlight: Bool { activeUploadCount > 0 }

    /// Called by a review window when its upload enters flight, so Sign Out is
    /// disabled until it finishes. Balanced by `uploadDidFinish()`.
    func uploadDidStart() { activeUploadCount += 1 }
    func uploadDidFinish() { activeUploadCount = max(0, activeUploadCount - 1) }
}
