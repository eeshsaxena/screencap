import AppKit
import Foundation

#if canImport(FoundationModels)
import FoundationModels
#endif

/// Live availability of Apple's built-in on-device model (Apple Foundation
/// Models). "On-device model" *is* Apple's `SystemLanguageModel`, gated at
/// runtime by the system-wide Apple Intelligence switch — so selecting it in
/// Intelligence Settings is not the same as it being usable. This is the single
/// enum both the Intelligence pane and Chat read to tell the user the *actual*
/// state (off / downloading / unsupported) instead of a generic "no model".
///
/// Read natively via `SystemLanguageModel.default.availability` — the same API
/// the `IntelligenceHelper` checks before generating — so there is no daemon
/// round-trip for a live badge. FoundationModels is macOS-26-only and weak-linked
/// (mirrors the helper target), so the probe is `#available`-guarded and degrades
/// to `.osUnsupported` on the app's macOS-13 floor.
enum OnDeviceModelStatus: Equatable, Sendable {
    /// Apple Intelligence is enabled and the model is ready — on-device answers work.
    case available
    /// Apple Intelligence is switched off system-wide (System Settings › Apple
    /// Intelligence & Siri). The one case the user fixes with a system toggle.
    case appleIntelligenceOff
    /// Apple Intelligence is on but the model is still downloading — ready shortly.
    case modelDownloading
    /// This Mac isn't eligible for Apple Intelligence at all (hardware/region).
    case notEligible
    /// The OS is below the macOS-26 floor, so Apple Foundation Models can't run.
    case osUnsupported
    /// Could not determine the state (probe unavailable / a future unknown reason).
    case unknown

    /// Probe the live system availability. Synchronous (the underlying property is
    /// a cheap read, no generation). The single line that touches the system —
    /// isolated here so the presentation logic (`settingsBadge`, the Chat guidance)
    /// stays pure and fully unit-testable.
    static func probe() -> OnDeviceModelStatus {
        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            switch SystemLanguageModel.default.availability {
            case .available:
                return .available
            case .unavailable(let reason):
                switch reason {
                case .appleIntelligenceNotEnabled:
                    return .appleIntelligenceOff
                case .modelNotReady:
                    return .modelDownloading
                case .deviceNotEligible:
                    return .notEligible
                @unknown default:
                    return .unknown
                }
            }
        }
        #endif
        return .osUnsupported
    }
}

/// The compact status chip shown on the on-device row in Intelligence Settings.
/// Pure/`Sendable` — the view maps `Tone` to a color; kept UI-framework-free so
/// the mapping is unit-tested without SwiftUI.
struct OnDeviceStatusBadge: Equatable, Sendable {
    enum Tone: Equatable, Sendable { case ok, warn, info }
    let text: String
    let tone: Tone
    /// Whether to offer the "Turn on in System Settings" affordance beside the chip
    /// — only for the one state a system toggle fixes (Apple Intelligence off).
    let offersSystemSettings: Bool
}

extension OnDeviceModelStatus {
    /// The status chip for the Intelligence-pane on-device row, or `nil` when the
    /// state can't be determined (show nothing rather than guess).
    var settingsBadge: OnDeviceStatusBadge? {
        switch self {
        case .available:
            return OnDeviceStatusBadge(text: "Ready", tone: .ok, offersSystemSettings: false)
        case .appleIntelligenceOff:
            return OnDeviceStatusBadge(
                text: "Apple Intelligence is off", tone: .warn, offersSystemSettings: true
            )
        case .modelDownloading:
            return OnDeviceStatusBadge(text: "Model downloading…", tone: .info, offersSystemSettings: false)
        case .notEligible:
            return OnDeviceStatusBadge(
                text: "Not supported on this Mac", tone: .warn, offersSystemSettings: false
            )
        case .osUnsupported:
            return OnDeviceStatusBadge(text: "Needs macOS 26", tone: .info, offersSystemSettings: false)
        case .unknown:
            return nil
        }
    }
}

/// Deep-link to System Settings › Apple Intelligence & Siri — the shared CTA for
/// the on-device "Apple Intelligence is off" state (used by both Chat and the
/// Intelligence pane). Falls back to opening System Settings at its root if the OS
/// doesn't recognize the anchor (mirrors `PermissionController`'s open-with-fallback).
enum AppleIntelligenceSettings {
    static func open() {
        let anchors = [
            "x-apple.systempreferences:com.apple.Siri-Settings.extension",
            "x-apple.systempreferences:",
        ]
        for raw in anchors {
            if let url = URL(string: raw), NSWorkspace.shared.open(url) { return }
        }
    }
}
