import Foundation

/// The live "usable" verdict — composed **app-side** (U5, KTD1/KTD3) from the fresh
/// Apple-Intelligence probe (`OnDeviceModelStatus`, a Swift-only fact) plus the
/// daemon's config facts (`IntelligenceSettings`). The daemon never probes
/// availability; the app owns composition, so "Ready" means results.
///
/// One logical verdict feeds every surface that gates on "can intelligence actually
/// run" — the first-recording beat's adaptive content, the dead-state banner, the
/// settings readiness label, and the empty-recording reason — so they cannot drift
/// (R8). Pure/`Sendable`, so the gate stays unit-testable without SwiftUI.
struct IntelligenceVerdict: Equatable, Sendable {
    /// Which engine would actually produce results (informational; `usable` is the gate).
    enum Target: Equatable, Sendable { case onDevice, cloud, none }

    let usable: Bool
    let target: Target

    /// Compose the verdict from the fresh probe + config facts.
    ///
    /// Returns `nil` when `settings` haven't loaded yet (the async config-facts read
    /// hasn't returned): the **unresolved** window resolves to "unknown", never
    /// "not set up" (KTD3) — nil facts mean "don't know yet", not "not usable".
    ///
    /// On-device is usable when Apple Intelligence is ready (`probe == .available`)
    /// OR a model is downloaded; a configured cloud provider with summary consent is
    /// usable for naming. The provider *preference* doesn't change usability, only
    /// which target would run.
    static func compose(probe: OnDeviceModelStatus, settings: IntelligenceSettings?) -> IntelligenceVerdict? {
        guard let s = settings else { return nil }  // unresolved → caller renders "unknown"
        let onDeviceUsable = probe == .available || s.downloadedModelInstalled
        let cloudUsable = (s.cloudProvider?.isEmpty == false) && s.summaryCloudConsent
        if onDeviceUsable { return IntelligenceVerdict(usable: true, target: .onDevice) }
        if cloudUsable { return IntelligenceVerdict(usable: true, target: .cloud) }
        return IntelligenceVerdict(usable: false, target: .none)
    }
}

/// The honest state for a recording's task display (R7 + KTD6). Composed from the
/// daemon-persisted per-recording `reason` plus the live verdict, with an explicit
/// precedence: a recorded outcome always wins; only its absence falls back to the
/// verdict-derived `notSetUp`, and an unresolved/usable verdict yields `unknown`
/// (never a false `notSetUp`).
///
/// `mechanicalOnly` and `producedTasksPartial` are special: the recording HAS tasks
/// (heuristic-named, or a model/mechanical mix — SCR-275 U7), so their UI is a
/// banner above the populated list, not an empty-state string (U6). Both carry
/// `sessionTooLong`: true when the daemon's degradation `detail` was
/// `context-window` — the session was too long for the on-device model — so the
/// banner can say WHY naming degraded (R9/KTD-8) instead of implying intelligence
/// isn't set up.
enum RecordingHonestState: Equatable, Sendable {
    case producedTasks    // AI-named tasks — normal
    /// SCR-275 U7: some tasks model-named, the rest mechanical → honest-mix banner.
    case producedTasksPartial(sessionTooLong: Bool)
    /// Tasks exist but ALL heuristic-named → "mechanical names" banner.
    case mechanicalOnly(sessionTooLong: Bool)
    case inProgress       // live/incremental segmentation not finished yet — transient
    case nothingToName    // a model ran and found nothing — quiet, no action
    case couldntRun       // the attempt failed and no fallback produced anything — retry
    case notSetUp         // app-derived: no usable model AND no recorded outcome — set up
    case unknown          // legacy recording / older daemon / unresolved verdict — neutral

    /// - Parameters:
    ///   - reason: the daemon's per-recording outcome string (`nil` = none recorded).
    ///   - detail: the daemon's distinct degradation reason (SCR-275 U6, additive —
    ///     `nil` on legacy rows / older daemons). Only `context-window` changes the
    ///     surface ("session too long for the on-device model"); any other value —
    ///     including future vocabulary — reads as a generic degradation.
    ///   - verdict: the composed live verdict (`nil` = unresolved → `unknown`).
    static func resolve(
        reason: String?, detail: String? = nil, verdict: IntelligenceVerdict?
    ) -> RecordingHonestState {
        let sessionTooLong = detail == "context-window"
        switch reason {
        case "produced_tasks": return .producedTasks
        case "produced_tasks_partial": return .producedTasksPartial(sessionTooLong: sessionTooLong)
        case "mechanical_only": return .mechanicalOnly(sessionTooLong: sessionTooLong)
        case "in_progress": return .inProgress
        case "nothing_to_name": return .nothingToName
        case "couldnt_run": return .couldntRun
        default:
            // No persisted outcome. Only a resolved, not-usable verdict yields
            // notSetUp; an unresolved (nil) or usable verdict → unknown (KTD6).
            guard let v = verdict else { return .unknown }
            return v.usable ? .unknown : .notSetUp
        }
    }

    /// Whether this state carries a populated task list (`mechanicalOnly` and the
    /// SCR-275 partial mix), so the view attaches a banner above the list rather
    /// than replacing an empty state.
    var hasPopulatedTasks: Bool {
        switch self {
        case .mechanicalOnly, .producedTasksPartial: return true
        case .producedTasks, .inProgress, .nothingToName, .couldntRun, .notSetUp, .unknown:
            return false
        }
    }

    /// Whether this state offers a set-up / finish-setup affordance (R7). A
    /// `sessionTooLong` degradation does NOT: intelligence ran and hit the model's
    /// context window, so "set up" would be a false diagnosis (R9). The partial mix
    /// never does — the model demonstrably named some tasks.
    var offersSetup: Bool {
        switch self {
        case .notSetUp: return true
        case .mechanicalOnly(let sessionTooLong): return !sessionTooLong
        case .producedTasks, .producedTasksPartial, .inProgress, .nothingToName,
             .couldntRun, .unknown:
            return false
        }
    }
}
