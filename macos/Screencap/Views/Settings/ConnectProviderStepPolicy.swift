import Foundation

// U4 (Intelligence pane redesign) — the sequential add-provider flow's pure
// decision layer (KTD4, mirroring `OnboardingStepPolicy`). The overlay sheet
// owns presentation and the controller calls; every transition and ordering
// rule lives here so it is unit-testable without a render tree:
//
// - Steps: pick → configure(choice) → done (R6). Add-new enters at pick;
//   Manage/Set-up enters at configure with the vendor (or local-server) preset.
// - Back from configure returns to pick ONLY for the add-new entry — a manage
//   entry never saw a pick step, so it has nothing to go back to.
// - Done is reachable only after a terminal configure action (`Completion` is
//   only ever minted by one), and finishing auto-selects the added row ONLY
//   when it is selectable per `IntelligenceSelectionModel` (R8): a
//   REMOTE-classified local server or a needs-attention CLI add finishes
//   highlighted but unselected.
// - Disconnect and endpoint writes are ordered plans: deselect-first for the
//   currently-selected key vendor; provider-reset-first for an endpoint clear
//   or re-save while `local-server` is the persisted provider.

/// How the flow was opened. Drives the initial step and whether the configure
/// step offers a Back to the pick step.
enum ConnectFlowEntry: Equatable {
    /// "Add another provider…" — starts at the pick step.
    case addNew
    /// A row's Manage/Set up/Connect accessory — enters directly at the
    /// configure step with the row's choice preset (no pick step behind it).
    case manage(ProviderChoice)
}

/// The flow's two visible steps. Done is not a step — it is the terminal
/// transition out of configure (the sheet closes and reports the added row).
enum ConnectFlowStep: Equatable {
    case pick
    case configure(ProviderChoice)
}

enum ConnectProviderStepPolicy {

    // MARK: - Step transitions (R6)

    /// The step the flow opens on: add-new starts at pick; a manage entry lands
    /// directly on its preset's configure step.
    static func initialStep(for entry: ConnectFlowEntry) -> ConnectFlowStep {
        switch entry {
        case .addNew:
            return .pick
        case .manage(let choice):
            return .configure(choice)
        }
    }

    /// Picking a choice advances to its configure step — the only transition
    /// out of pick (no step skips; done is unreachable from pick).
    static func step(afterPicking choice: ProviderChoice) -> ConnectFlowStep {
        .configure(choice)
    }

    /// Back exists only on the configure step of an add-new entry — a manage
    /// entry enters at configure and has no pick step to return to.
    static func canGoBack(from step: ConnectFlowStep, entry: ConnectFlowEntry) -> Bool {
        guard case .configure = step, case .addNew = entry else { return false }
        return true
    }

    /// The step Back returns to, or nil when Back is not offered.
    static func stepAfterBack(
        from step: ConnectFlowStep, entry: ConnectFlowEntry
    ) -> ConnectFlowStep? {
        canGoBack(from: step, entry: entry) ? .pick : nil
    }

    /// Done is reachable only from a configure step — and even there only after
    /// a terminal configure action minted a `Completion` (the sheet gates its
    /// Done affordance on holding one).
    static func canFinish(from step: ConnectFlowStep) -> Bool {
        guard case .configure = step else { return false }
        return true
    }

    // MARK: - Completion (R8 — auto-select only selectable rows)

    /// What finishing the flow does for the added row: the row id reported to
    /// the pane (which sets the just-added highlight) plus the auto-select the
    /// sheet issues through the controller seams — `.none` when the row is not
    /// selectable per `IntelligenceSelectionModel` (R8/R9).
    struct Completion: Equatable {
        let addedRowID: String
        let autoSelect: AutoSelect
    }

    /// The controller seam a completion's auto-select routes through (KTD1/KTD2:
    /// BYO rows write only the cloud slot; local rows go through the
    /// clear-cloud-first `selectLocalProvider` seam).
    enum AutoSelect: Equatable {
        /// `selectCloudProvider(id)` — a selectable BYO row.
        case cloudRow(id: String)
        /// `selectLocalProvider(provider)` — the LOCAL-classified server row.
        case localRow(provider: String)
        /// Row not selectable — finish highlighted but unselected (R8).
        case none
    }

    /// A stored (valid or unverified-unknown) key makes the vendor's key row
    /// connected and therefore selectable — auto-select it.
    static func completion(afterKeyStored vendor: BYOVendor) -> Completion {
        Completion(
            addedRowID: vendor.keyProviderID,
            autoSelect: .cloudRow(id: vendor.keyProviderID)
        )
    }

    /// A CLI add auto-selects only when the delegation CLI is available; a
    /// needs-attention CLI add finishes without selection (R8).
    static func completion(
        afterCLIConfirmed vendor: BYOVendor, cliAvailable: Bool
    ) -> Completion {
        Completion(
            addedRowID: vendor.cliProviderID,
            autoSelect: cliAvailable ? .cloudRow(id: vendor.cliProviderID) : .none
        )
    }

    /// An endpoint save auto-selects the local-server row only when the daemon
    /// classified it LOCAL (AE3/R9); REMOTE — or an unreadable classification,
    /// which the daemon treats as not-local — finishes highlighted but
    /// unselected, with copy stating that answers and day-splitting require a
    /// local endpoint.
    static func completion(afterEndpointSaved classification: String?) -> Completion {
        Completion(
            addedRowID: IntelligenceSelectionModel.localServerRowID,
            autoSelect: classification == "LOCAL"
                ? .localRow(provider: IntelligenceSelectionModel.localServerRowID)
                : .none
        )
    }

    // MARK: - Disconnect plan (deselect-first)

    /// One ordered operation of a key disconnect.
    enum DisconnectOperation: Equatable {
        /// `selectLocalProvider("on-device")` — selection visibly returns to
        /// on-device BEFORE the key disappears.
        case selectOnDevice
        /// `clearBYOKey(vendor:)`.
        case clearKey(vendor: String)
    }

    /// Disconnecting a vendor's key while its key row is the persisted
    /// `cloud_provider` deselects FIRST (selection returns to on-device), then
    /// clears the key — never a moment where a consented cloud slot points at a
    /// vendor whose key is already gone. A non-selected vendor just clears.
    static func disconnectPlan(
        vendor: BYOVendor, persistedCloudProvider: String?
    ) -> [DisconnectOperation] {
        if persistedCloudProvider == vendor.keyProviderID {
            return [.selectOnDevice, .clearKey(vendor: vendor.rawValue)]
        }
        return [.clearKey(vendor: vendor.rawValue)]
    }

    // MARK: - Endpoint write plan (provider-reset-first)

    /// One ordered operation of an endpoint save or clear.
    enum EndpointOperation: Equatable {
        /// `selectLocalProvider("on-device")` — issued BEFORE the endpoint write.
        case selectOnDevice
        /// `setEndpoint(value)` — an empty value clears (the controller maps it
        /// to the CLI's `none`).
        case writeEndpoint(String)
    }

    /// The ordered writes for an endpoint save or clear. While `local-server`
    /// is the persisted provider, the plan resets to on-device FIRST: a new
    /// URL's LOCAL/REMOTE classification is only known after the daemon's
    /// (syntactic, KTD5) round-trip, and the active provider must never point
    /// at a REMOTE or absent endpoint even transiently. A save that comes back
    /// LOCAL re-selects the row via `completion(afterEndpointSaved:)`'s
    /// auto-select; a clear or REMOTE result stays on on-device.
    static func endpointWritePlan(
        newValue: String, persistedProvider: String
    ) -> [EndpointOperation] {
        if persistedProvider == IntelligenceSelectionModel.localServerRowID {
            return [.selectOnDevice, .writeEndpoint(newValue)]
        }
        return [.writeEndpoint(newValue)]
    }
}
