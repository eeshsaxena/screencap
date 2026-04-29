import AppKit
import AVFoundation
import ApplicationServices
import Combine
import CoreGraphics
import Foundation

/// macOS Privacy & Security panes targeted by the deep-link helpers.
/// macOS 13+ uses the `.extension` URL form; older forms hit a generic page on macOS 26.
enum PrivacyPane: String, CaseIterable {
    case screenRecording
    case accessibility
    case microphone

    var deepLinkURL: URL {
        let host = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"
        switch self {
        case .screenRecording: return URL(string: "\(host)?Privacy_ScreenCapture")!
        case .accessibility:   return URL(string: "\(host)?Privacy_Accessibility")!
        case .microphone:      return URL(string: "\(host)?Privacy_Microphone")!
        }
    }

    /// Fallback URL when the .extension form is unavailable on the target macOS.
    var fallbackURL: URL {
        URL(string: "x-apple.systempreferences:com.apple.preference.security")!
    }

    var displayName: String {
        switch self {
        case .screenRecording: return "Screen Recording"
        case .accessibility:   return "Accessibility"
        case .microphone:      return "Microphone"
        }
    }

    var rationale: String {
        switch self {
        case .screenRecording: return "Required to capture your screen."
        case .accessibility:   return "Required to record keyboard and mouse events."
        case .microphone:      return "Optional. Enables audio recording alongside the screen."
        }
    }

    var isRequired: Bool {
        self != .microphone
    }
}

enum PermissionStatus: Equatable {
    case granted
    case denied
    case notDetermined

    var isGranted: Bool { self == .granted }
}

@MainActor
final class PermissionController: ObservableObject {
    @Published private(set) var screenRecording: PermissionStatus = .notDetermined
    @Published private(set) var accessibility: PermissionStatus = .notDetermined
    @Published private(set) var microphone: PermissionStatus = .notDetermined

    private var pollTimer: Timer?
    private var workspaceObserver: NSObjectProtocol?

    var allRequiredGranted: Bool {
        screenRecording == .granted && accessibility == .granted
    }

    var anyDenied: Bool {
        screenRecording == .denied || accessibility == .denied
    }

    init() {
        refresh()
    }

    deinit {
        // Timers and observers must be cleaned up on deinit to avoid leaks.
        pollTimer?.invalidate()
        if let workspaceObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver)
        }
    }

    /// Starts the live polling loop used by FirstRunPermissionsView while it is visible.
    func startWatching() {
        stopWatching()
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        pollTimer = timer

        workspaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func stopWatching() {
        pollTimer?.invalidate()
        pollTimer = nil
        if let workspaceObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver)
            self.workspaceObserver = nil
        }
    }

    /// Recomputes all three permission statuses without prompting.
    func refresh() {
        screenRecording = Self.checkScreenRecording()
        accessibility = Self.checkAccessibility()
        microphone = Self.checkMicrophone()
    }

    /// Opens System Settings to the requested pane. Tries the macOS 13+ .extension
    /// URL first; falls back to the generic Privacy & Security page if unavailable.
    func openSystemSettings(for pane: PrivacyPane) {
        if !NSWorkspace.shared.open(pane.deepLinkURL) {
            NSWorkspace.shared.open(pane.fallbackURL)
        }
    }

    // MARK: - Silent checks

    private static func checkScreenRecording() -> PermissionStatus {
        // CGPreflightScreenCaptureAccess returns Bool; no notDetermined distinction.
        CGPreflightScreenCaptureAccess() ? .granted : .denied
    }

    private static func checkAccessibility() -> PermissionStatus {
        let options: NSDictionary = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false]
        return AXIsProcessTrustedWithOptions(options) ? .granted : .denied
    }

    private static func checkMicrophone() -> PermissionStatus {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:        return .granted
        case .denied, .restricted: return .denied
        case .notDetermined:     return .notDetermined
        @unknown default:        return .notDetermined
        }
    }
}
