import Foundation

/// U1 (Intelligence pane redesign) — the pure models behind the redesigned
/// MODEL list: grouping, rendered selection, write routing, the on-device row
/// state matrix, and the nudge/highlight predicates.
///
/// Factored out ahead of the pane restructure (U3) so every new behavior rule
/// is unit-testable without a running SwiftUI hierarchy (KTD4 — there is no UI
/// test tooling in this repo). Nothing here touches the network or the CLI;
/// `IntelligenceController` performs the actual writes.
///
/// Rules encoded, with their contract ids:
/// - Grouping: "Included with ScreenCap" (on-device only, no placeholder for
///   the reserved hosted slot — R5) and "Your own" (derived membership — R1).
/// - Rendered selection with the legacy reconcile treatment (KTD1).
/// - Write routing by row kind + the persisted-value tap guard (KTD1/KTD2).
/// - REMOTE-endpoint rows render but are not selectable (R9 / AE3).
/// - The merged on-device row's state matrix (KTD3).
/// - The standing consent nudge and just-added highlight predicates (KTD4/KTD7).

// MARK: - Rows and groups

/// One row of the grouped MODEL list. `kind` drives write routing;
/// `selectable` gates radio interaction (REMOTE local servers and
/// needs-attention BYO rows render but cannot be picked — R9/R8).
struct IntelligenceModelRow: Identifiable, Equatable {
    enum Kind: Equatable {
        /// The merged on-device row (Apple Intelligence + downloaded model —
        /// the daemon's `ChainedOnDeviceProvider` resolves which one runs, KTD2).
        case onDevice
        /// The user's own local server (Ollama / LM Studio).
        case localServer
        /// A bring-your-own cloud provider, selected as `cloud_provider` only.
        case byo(providerID: String)
    }

    let id: String
    let kind: Kind
    let title: String
    let subtitle: String?
    let selectable: Bool
}

/// An ownership group of the MODEL list (R1 — whose infrastructure and whose
/// bill each option uses, legible at a glance).
struct IntelligenceModelGroup: Identifiable, Equatable {
    let id: String
    let title: String
    let rows: [IntelligenceModelRow]
}

// MARK: - Rendered selection

/// The single row the list renders as selected, mapped from the persisted
/// two-slot state (KTD1). `needsReconcile` marks a legacy/inconsistent persisted
/// value (`gemini` on `provider`, `local-server` with no or a REMOTE endpoint):
/// the on-device row renders selected but with a needs-attention treatment
/// prompting a re-pick — never plain Ready, since the daemon routes such values
/// to the idle-gap heuristic.
struct RenderedModelSelection: Equatable {
    let rowID: String
    let needsReconcile: Bool
}

// MARK: - Write instructions

/// One ordered write the controller must issue for a row pick (KTD1). A local
/// row clears the cloud slot FIRST, then sets `provider`; a BYO row writes only
/// `cloud_provider` (the daemon hard-rejects BYO ids on `provider`).
enum ProviderWriteInstruction: Equatable {
    /// `setCloudProvider("none")` — must precede the `setProvider` write.
    case clearCloudProvider
    /// `setProvider(value)` — always `"on-device"` or `"local-server"`.
    case setProvider(String)
    /// `setCloudProvider(id)` — a BYO pick touches only the cloud slot.
    case setCloudProvider(String)
}

// MARK: - On-device row state matrix (KTD3)

/// The pure render of the merged on-device row over
/// (Apple probe × downloaded-installed × download state × daemon reachability).
/// `status` is the row's badge/primary state; `affordance` is the download UI;
/// `affordanceDemoted` is true when a local engine is already ready or arriving,
/// so the affordance renders secondary (never competing with Ready).
struct OnDeviceRowRender: Equatable {
    enum Status: Equatable {
        /// Apple Intelligence answers — "Ready (Apple Intelligence)".
        case readyApple
        /// The downloaded model answers — "Ready (downloaded model)", no warning.
        case readyDownloaded
        /// Apple's own model is still downloading — info tone, not needs-attention.
        case appleModelDownloading
        /// No local engine can run (KTD3's only warning condition).
        /// `offersSystemSettings` is true only for the one state a system toggle
        /// fixes (Apple Intelligence off).
        case needsAttention(offersSystemSettings: Bool)
        /// Probe unresolved — neutral checking state, no warning.
        case checking
        /// No badge — the download affordance (progress/failure) is the row's
        /// primary content.
        case plain
    }

    enum DownloadAffordance: Equatable {
        /// Model installed — nothing to download.
        case hidden
        /// The Download button.
        case download
        /// Progress bar + Cancel (poll running).
        case progressCancel
        /// Failure reason + Retry (insufficient disk surfaces as the reason).
        case retry(reason: String)
        /// Daemon unreachable — the affordance is disabled with a
        /// start-the-daemon reason (any probe × any install state).
        case disabled(reason: String)
    }

    let status: Status
    let affordance: DownloadAffordance
    let affordanceDemoted: Bool
}

// MARK: - Just-added highlight (R8)

/// The just-added row highlight's transition rule. The view owns invocation
/// (flow completion, selection changes, pane disappear); this owns when the
/// highlight sets and clears so the rule is unit-testable.
struct JustAddedHighlight: Equatable {
    private(set) var rowID: String?

    init(rowID: String? = nil) {
        self.rowID = rowID
    }

    /// Flow completion sets the highlight on the added row (R8).
    mutating func flowCompleted(addedRowID: String) {
        rowID = addedRowID
    }

    /// A selection change clears the highlight — unless the new selection IS the
    /// highlighted row: flow completion auto-selects the added row when it is
    /// selectable (R8), and that auto-select must not erase its own chip.
    mutating func selectionChanged(to newRowID: String?) {
        guard let current = rowID, newRowID != current else { return }
        rowID = nil
    }

    /// Leaving the pane clears the highlight.
    mutating func paneDisappeared() {
        rowID = nil
    }

    func isHighlighted(_ id: String) -> Bool {
        rowID == id
    }
}

// MARK: - The model

/// Namespace for the pure rules. Copy lives in statics so the honest-copy audit
/// can reach every string (KTD7).
enum IntelligenceSelectionModel {

    // MARK: Row/provider identifiers

    /// Row id and `provider set` value for the on-device row (KTD2 — always
    /// `on-device`, never `downloaded`; the daemon's chain resolves the engine).
    static let onDeviceRowID = "on-device"
    /// Row id and `provider set` value for the local-server row.
    static let localServerRowID = "local-server"

    // MARK: Copy (KTD7 — statics so the audit corpus can enumerate them)

    static let includedGroupTitle = "Included with ScreenCap"
    static let yourOwnGroupTitle = "Your own"
    static let onDeviceRowTitle = "On-device model"
    static let onDeviceRowSubtitle = "Runs on this Mac · nothing leaves"
    static let localServerRowTitle = "Local server"
    /// AE3/R9 — the stated consequence of a REMOTE-classified endpoint.
    static let remoteEndpointNotSelectableCopy =
        "Remote endpoint — answers and day-splitting require a local endpoint."
    /// KTD3 — the disabled-download reason when the model status read fails.
    static let startDaemonDownloadReason =
        "Can't reach the daemon — start the daemon to manage the model download."

    // MARK: Grouped options (R1/R5)

    /// Build the two ownership groups from a settings read-back.
    ///
    /// "Included with ScreenCap" holds only the on-device row — the hosted-cloud
    /// slot is reserved with no placeholder (R5). "Your own" holds every BYO row
    /// passing the derived membership rule
    /// (`keyPresent || cliAvailable || endpointSet || isCurrentCloudProvider`)
    /// plus the local-server row when an endpoint is set; the Add affordance is
    /// the view's, not a row here.
    static func groups(_ settings: IntelligenceSettings) -> [IntelligenceModelGroup] {
        [
            IntelligenceModelGroup(
                id: "included",
                title: includedGroupTitle,
                rows: [onDeviceRow()]
            ),
            IntelligenceModelGroup(
                id: "your-own",
                title: yourOwnGroupTitle,
                rows: yourOwnRows(settings)
            ),
        ]
    }

    private static func onDeviceRow() -> IntelligenceModelRow {
        IntelligenceModelRow(
            id: onDeviceRowID,
            kind: .onDevice,
            title: onDeviceRowTitle,
            subtitle: onDeviceRowSubtitle,
            selectable: true
        )
    }

    /// The "Your own" rows: BYO vendor rows (key + CLI, reusing the
    /// `ConnectProviderModel` option matrix for titles and connection state),
    /// then the local-server row last. A row is a member when it is connected
    /// OR it is the persisted `cloud_provider` (so a stranded selection stays
    /// visible as needs-attention instead of vanishing); it is selectable only
    /// when connected (R8 — a needs-attention row finishes highlighted but
    /// unselected).
    private static func yourOwnRows(_ settings: IntelligenceSettings) -> [IntelligenceModelRow] {
        var rows: [IntelligenceModelRow] = []
        for option in ConnectProviderModel.userOwnedOptions(settings)
        where option.state == .connected || option.isSelected {
            rows.append(IntelligenceModelRow(
                id: option.providerID,
                kind: .byo(providerID: option.providerID),
                title: option.title,
                subtitle: nil,
                selectable: option.state == .connected
            ))
        }
        if let endpoint = trimmedNonEmpty(settings.localServerEndpoint) {
            let isLocal = settings.endpointClassification == "LOCAL"
            rows.append(IntelligenceModelRow(
                id: localServerRowID,
                kind: .localServer,
                title: localServerRowTitle,
                // R9/AE3 — a REMOTE row renders with the consequence stated.
                subtitle: isLocal
                    ? "\(endpoint) · runs on this Mac"
                    : "\(endpoint) · \(remoteEndpointNotSelectableCopy)",
                selectable: isLocal
            ))
        }
        return rows
    }

    // MARK: Rendered selection (KTD1 read path)

    /// Map the persisted two-slot state to the single rendered selection:
    /// `cloud_provider` set → that BYO row; else `provider` `on-device`/
    /// `downloaded` → the on-device row; `local-server` with a LOCAL endpoint →
    /// the local-server row; anything else (legacy `gemini`, `local-server`
    /// with no/REMOTE endpoint) → the on-device row with the reconcile
    /// treatment. Render-only — no write-through migration happens here.
    static func renderedSelection(_ settings: IntelligenceSettings) -> RenderedModelSelection {
        if let cloud = normalizedCloudProvider(settings.cloudProvider) {
            return RenderedModelSelection(rowID: cloud, needsReconcile: false)
        }
        switch settings.provider {
        case "on-device", "downloaded":
            // `downloaded` is a valid engine but no longer a row of its own —
            // the on-device row covers it (KTD2).
            return RenderedModelSelection(rowID: onDeviceRowID, needsReconcile: false)
        case "local-server" where hasLocalEndpoint(settings):
            return RenderedModelSelection(rowID: localServerRowID, needsReconcile: false)
        default:
            return RenderedModelSelection(rowID: onDeviceRowID, needsReconcile: true)
        }
    }

    private static func hasLocalEndpoint(_ settings: IntelligenceSettings) -> Bool {
        trimmedNonEmpty(settings.localServerEndpoint) != nil
            && settings.endpointClassification == "LOCAL"
    }

    // MARK: Write routing (KTD1 write path)

    /// The ordered writes a pick of `kind` issues. A local row clears the cloud
    /// slot FIRST so no state ever holds both slots pointing at different
    /// answerers; the on-device row ALWAYS writes `on-device` regardless of
    /// probe state — the daemon's `ChainedOnDeviceProvider` cascades
    /// Apple → downloaded at generation time, and writing `downloaded` would pin
    /// a stale probe snapshot (KTD2).
    static func writes(forPick kind: IntelligenceModelRow.Kind) -> [ProviderWriteInstruction] {
        switch kind {
        case .onDevice:
            return [.clearCloudProvider, .setProvider(onDeviceRowID)]
        case .localServer:
            return [.clearCloudProvider, .setProvider(localServerRowID)]
        case .byo(let providerID):
            return [.setCloudProvider(providerID)]
        }
    }

    /// The tap no-op guard (KTD1): compare the values a tap WOULD WRITE against
    /// the *persisted* `provider`/`cloud_provider` — never the rendered
    /// selection. Tapping the rendered-selected on-device row while disk holds a
    /// legacy value (`gemini`, `downloaded`) therefore performs the write-through
    /// and heals the divergence; tapping it while disk already holds `on-device`
    /// is a no-op (empty write list).
    static func tapWrites(
        forPick kind: IntelligenceModelRow.Kind,
        persistedProvider: String,
        persistedCloudProvider: String?
    ) -> [ProviderWriteInstruction] {
        let cloud = normalizedCloudProvider(persistedCloudProvider)
        switch kind {
        case .onDevice:
            if cloud == nil && persistedProvider == onDeviceRowID { return [] }
        case .localServer:
            if cloud == nil && persistedProvider == localServerRowID { return [] }
        case .byo(let providerID):
            // A BYO pick writes only the cloud slot, so only that slot is compared.
            if cloud == providerID { return [] }
        }
        return writes(forPick: kind)
    }

    /// The persisted cloud slot, normalized: nil/empty/"none" all mean unset
    /// (the CLI clears with the literal `none`; the read-back should emit null,
    /// but the mapping stays total either way).
    private static func normalizedCloudProvider(_ raw: String?) -> String? {
        guard let value = trimmedNonEmpty(raw), value != "none" else { return nil }
        return value
    }

    private static func trimmedNonEmpty(_ raw: String?) -> String? {
        guard let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else { return nil }
        return trimmed
    }

    // MARK: On-device row state matrix (KTD3)

    /// Render the merged on-device row. Pure over the four inputs; the matrix in
    /// the plan's Planning Contract is the spec, encoded row for row:
    ///
    /// - `.cancelled` renders exactly as `.idle` (its rows say "same as idle").
    /// - Installed always beats a not-ready probe: a local engine can run, so
    ///   the row is Ready (downloaded model) with no warning.
    /// - Needs-attention only when no local engine can run AND no download is
    ///   in flight; a downloading/failed download becomes the row's primary
    ///   content (`.plain` status) instead.
    /// - `unknown` probe with a download in flight/failed is treated like
    ///   not-ready (the matrix pins only unknown × idle → checking; showing the
    ///   user's own download promoted is the consistent extension).
    /// - Daemon unreachable overrides the affordance for any probe × any
    ///   install state (the status read is the only source of download truth).
    static func onDeviceRowRender(
        probe: OnDeviceModelStatus,
        installed: Bool,
        download: ModelDownloadState,
        daemonUnreachable: Bool
    ) -> OnDeviceRowRender {
        // A terminal `.installed` download state is the installed flag by
        // another name; `.cancelled` renders exactly as `.idle`.
        var installed = installed
        if case .installed = download { installed = true }
        let effectiveDownload: ModelDownloadState
        switch download {
        case .cancelled, .installed:
            effectiveDownload = .idle
        default:
            effectiveDownload = download
        }

        let status = statusFor(
            probe: probe, installed: installed, download: effectiveDownload
        )

        let affordance: OnDeviceRowRender.DownloadAffordance
        if daemonUnreachable {
            affordance = .disabled(reason: startDaemonDownloadReason)
        } else if installed {
            affordance = .hidden
        } else {
            switch effectiveDownload {
            case .downloading:
                affordance = .progressCancel
            case .failed(let reason):
                affordance = .retry(reason: reason)
            default:
                affordance = .download
            }
        }

        // Demoted whenever a local engine is ready or arriving (or the probe is
        // still resolving) — the affordance never competes with a Ready badge.
        let demoted: Bool
        switch status {
        case .readyApple, .readyDownloaded, .appleModelDownloading, .checking:
            demoted = true
        case .needsAttention, .plain:
            demoted = false
        }

        return OnDeviceRowRender(
            status: status, affordance: affordance, affordanceDemoted: demoted
        )
    }

    private static func statusFor(
        probe: OnDeviceModelStatus,
        installed: Bool,
        download: ModelDownloadState
    ) -> OnDeviceRowRender.Status {
        if installed {
            // Apple ready still fronts the row; otherwise the downloaded model
            // is the ready engine — no warning, whatever the probe says.
            return probe == .available ? .readyApple : .readyDownloaded
        }
        switch probe {
        case .available:
            return .readyApple
        case .modelDownloading:
            // Apple's own model download — info tone, never needs-attention.
            return .appleModelDownloading
        case .appleIntelligenceOff, .notEligible, .osUnsupported, .unknown:
            switch download {
            case .downloading, .failed:
                // The download UI is the primary content; no second badge.
                return .plain
            default:
                if probe == .unknown { return .checking }
                // The one state a system toggle fixes offers the deep link.
                return .needsAttention(offersSystemSettings: probe == .appleIntelligenceOff)
            }
        }
    }

    // MARK: Consent nudge (AE1)

    /// The standing nudge renders while a cloud (BYO) row is the rendered
    /// selection and any relevant consent toggle is off — with both off it
    /// explains why nothing is sent anywhere (AE1); with both on, no nudge.
    /// Local rows never nudge (nothing leaves regardless of toggles).
    static func consentNudgeVisible(
        selectedRowID: String,
        summaryCloudConsent: Bool,
        recallCloudConsent: Bool
    ) -> Bool {
        guard ConnectProviderModel.isBYOProvider(selectedRowID) else { return false }
        return !(summaryCloudConsent && recallCloudConsent)
    }
}
