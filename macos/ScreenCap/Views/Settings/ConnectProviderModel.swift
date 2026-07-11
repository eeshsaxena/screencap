import Foundation

/// U6/U7 — the pure model behind the "connect your own account" flow and the
/// user-owned (BYO) option matrix of the Intelligence provider picker. (The
/// ownership *grouping* of the rendered MODEL list lives in
/// `IntelligenceSelectionModel.groups` since the U3 redesign.)
///
/// Factored out of the view so the load-bearing rules are unit-testable without
/// a running SwiftUI hierarchy:
///
/// - which vendors × mechanisms exist and what `cloud_provider` id each writes
///   (KTD2 — a BYO provider is only ever selected as the consented
///   `cloud_provider`, never the active `provider`);
/// - Gemini appears exactly once as a user-owned entry (R10);
/// - the honest delegation copy carries no forbidden marketing claim (R12).
///
/// None of this touches the network or the CLI — the view drives the actual
/// writes through `IntelligenceController`.

/// A cloud intelligence vendor the user can connect their own account to.
enum BYOVendor: String, CaseIterable, Identifiable {
    case openai
    case anthropic
    case gemini

    var id: String { rawValue }

    /// The human display name for the vendor (the company/product, not the id).
    var displayName: String {
        switch self {
        case .openai: return "OpenAI"
        case .anthropic: return "Anthropic"
        case .gemini: return "Gemini"
        }
    }

    /// The `cloud_provider` id written when connecting this vendor by API key.
    /// Matches `config._VALID_CLOUD_PROVIDERS` on the daemon.
    var keyProviderID: String { rawValue }

    /// The `cloud_provider` id written when delegating to this vendor's CLI.
    /// Matches `cli_delegate.VENDOR_IDS` on the daemon.
    var cliProviderID: String { "\(rawValue)-cli" }

    /// The vendor's CLI binary name, for the install/sign-in guidance (R5).
    var cliBinaryName: String {
        switch self {
        case .openai: return "codex"
        case .anthropic: return "claude"
        case .gemini: return "gemini"
        }
    }

    /// Install/sign-in guidance shown when the delegation CLI is unavailable
    /// (R5) or later reports needs-attention at runtime (R13). Honest about
    /// where the auth lives — ScreenCap holds no token (KTD1).
    var cliFixGuidance: String {
        switch self {
        case .openai:
            return "Install the Codex CLI and sign in with ChatGPT — ScreenCap invokes your signed-in `codex`, it never stores a token."
        case .anthropic:
            return "Install Claude Code and sign in — ScreenCap invokes your signed-in `claude`, it never stores a token."
        case .gemini:
            return "Install the Gemini CLI and sign in — ScreenCap invokes your signed-in `gemini`, it never stores a token."
        }
    }

    /// Honest delegation-limits copy (R12). Subscription usage is bounded by the
    /// provider's own limits/terms and can change; Gemini's free-tier caps are
    /// surfaced plainly. Deliberately no "free unlimited" / "we can't see it".
    var cliLimitsCopy: String {
        switch self {
        case .openai:
            return "Runs on your ChatGPT subscription via your `codex` CLI. Usage counts against your plan's limits and terms, which the provider can change."
        case .anthropic:
            return "Runs on your Claude subscription via your `claude` CLI. Usage counts against your plan's limits and terms, which the provider can change."
        case .gemini:
            return "Runs on your Google account via your `gemini` CLI. The free sign-in tier is rate-limited (about 5 requests/min, 100/day for Pro); a paid key lifts the caps but is metered per use."
        }
    }

    /// Honest API-key copy (R12). A metered pay-as-you-go key; no unlimited claim.
    var keyBillingCopy: String {
        switch self {
        case .gemini:
            return "Runs on your Google Gemini API key. Metered pay-as-you-go against your Google billing."
        default:
            return "Runs on your \(displayName) API key. Metered pay-as-you-go against your \(displayName) billing."
        }
    }
}

/// One choice on the add-provider flow's pick step (U4/R6): one of the three
/// BYO cloud vendors, or the user's own local server. The vendor list gains
/// Local server here because the flow — not the pane — now owns the endpoint.
enum ProviderChoice: Equatable, Identifiable {
    case vendor(BYOVendor)
    case localServer

    var id: String {
        switch self {
        case .vendor(let vendor): return vendor.rawValue
        case .localServer: return IntelligenceSelectionModel.localServerRowID
        }
    }

    /// The pick-step row title.
    var displayName: String {
        switch self {
        case .vendor(let vendor): return vendor.displayName
        case .localServer: return ConnectProviderModel.localServerChoiceTitle
        }
    }
}

/// The two ways to connect a vendor (R2).
enum BYOMechanism: String, CaseIterable, Identifiable {
    /// Paste a provider API key (stored daemon-side as a Keychain-class secret).
    case apiKey
    /// Delegate to the user's already-signed-in provider CLI (no token held).
    case cli

    var id: String { rawValue }

    var label: String {
        switch self {
        case .apiKey: return "Use my API key"
        case .cli: return "Use my subscription (via CLI)"
        }
    }
}

/// The runtime state of a single BYO connection slot, derived from the settings
/// read-back. Drives whether a row is connected, selectable, or needs attention.
enum BYOConnectionState: Equatable {
    /// Nothing connected yet for this vendor+mechanism.
    case notConnected
    /// Connected and healthy — selectable as the active cloud provider.
    case connected
    /// Connected/desired but the delegation CLI is unavailable — the R5
    /// (install-time absent) and R13 (metered/restricted mid-use) surface. Not
    /// selectable; the fix guidance is shown.
    case needsAttention
}

/// One presentable BYO option: a vendor × mechanism pairing plus its resolved
/// state and the `cloud_provider` id it selects.
struct BYOProviderOption: Identifiable, Equatable {
    let vendor: BYOVendor
    let mechanism: BYOMechanism
    /// The `cloud_provider` id this option writes when selected (KTD2).
    let providerID: String
    /// True when this option is the currently-selected `cloud_provider`.
    let isSelected: Bool
    let state: BYOConnectionState

    var id: String { providerID }

    /// Title shown in the "Your own account" section — "your OpenAI account", etc.
    var title: String {
        switch mechanism {
        case .apiKey: return "\(vendor.displayName) — your API key"
        case .cli: return "\(vendor.displayName) — your subscription"
        }
    }

    /// Whether the row can be selected as the active cloud provider right now.
    /// A CLI option must be available; a key option must have a stored key.
    var isSelectable: Bool {
        state == .connected
    }
}

/// Assembles the full user-owned BYO option matrix from a settings read-back.
/// The single source of truth for R9/R10/R12 membership + copy.
enum ConnectProviderModel {
    /// The vendors offered, in display order.
    static let vendors: [BYOVendor] = [.openai, .anthropic, .gemini]

    /// Build the six BYO options (3 vendors × 2 mechanisms) with their resolved
    /// state against `settings`. Gemini appears once per mechanism as a
    /// user-owned entry — never duplicated into the hosted section (R10).
    static func userOwnedOptions(_ settings: IntelligenceSettings) -> [BYOProviderOption] {
        var out: [BYOProviderOption] = []
        for vendor in vendors {
            // API-key mechanism.
            let keyID = vendor.keyProviderID
            let keyPresent = settings.keyPresent(forVendor: vendor.rawValue)
            out.append(BYOProviderOption(
                vendor: vendor,
                mechanism: .apiKey,
                providerID: keyID,
                isSelected: settings.cloudProvider == keyID,
                state: keyPresent ? .connected : .notConnected
            ))
            // CLI-delegation mechanism.
            let cliID = vendor.cliProviderID
            let cliAvailable = settings.cliAvailable(forProviderID: cliID)
            let isSelectedCli = settings.cloudProvider == cliID
            // Selected-but-unavailable = the R13 needs-attention state (metered /
            // restricted / signed-out mid-use). Unselected-and-unavailable is the
            // R5 install-time-absent state. Both reuse the same unavailable surface.
            let cliState: BYOConnectionState
            if cliAvailable {
                cliState = .connected
            } else {
                cliState = .needsAttention
            }
            out.append(BYOProviderOption(
                vendor: vendor,
                mechanism: .cli,
                providerID: cliID,
                isSelected: isSelectedCli,
                state: cliState
            ))
        }
        return out
    }

    /// True when `providerID` is any BYO (user-owned) cloud id — used to route a
    /// selection to `cloud_provider` (not the active `provider`, KTD2).
    static func isBYOProvider(_ providerID: String?) -> Bool {
        guard let providerID else { return false }
        return allBYOProviderIDs.contains(providerID)
    }

    /// Every BYO `cloud_provider` id (key + CLI), matching the daemon's
    /// `_VALID_CLOUD_PROVIDERS`.
    static let allBYOProviderIDs: [String] =
        vendors.map(\.keyProviderID) + vendors.map(\.cliProviderID)

    // MARK: - Add-provider flow (U4)

    /// The pick step's choices, in display order: the three BYO vendors plus
    /// the user's own local server (R6).
    static let flowChoices: [ProviderChoice] =
        vendors.map(ProviderChoice.vendor) + [.localServer]

    // MARK: - Flow copy (U4, KTD7 — statics so the honest-copy audit can
    // enumerate every string; fallback-honest: nothing here claims a cloud pick
    // replaces the local answerer)

    /// The pick step's title and caption.
    static let flowPickStepTitle = "Add a provider"
    static let flowPickCaption =
        "Connect a model that runs on your own account or hardware. It only handles the tasks you turn on."

    /// The configure step's title, per choice.
    static func flowConfigureStepTitle(for choice: ProviderChoice) -> String {
        switch choice {
        case .vendor(let vendor): return "Connect \(vendor.displayName)"
        case .localServer: return "Connect a local server"
        }
    }

    static let flowBackButtonTitle = "Back"
    static let flowDoneButtonTitle = "Done"

    /// Pick-step row captions.
    static let vendorChoiceCaption = "Connect with your API key or your signed-in CLI."
    static let localServerChoiceTitle = "Local server"
    static let localServerChoiceCaption =
        "Ollama or LM Studio running on this Mac — connect by URL."

    /// KTD5 — the CLI availability check is a local stat-check; the copy states
    /// plainly that no test call is made to the provider.
    static let cliAvailabilityHonestCopy =
        "Availability is checked on this Mac only — no test call is sent to the provider."

    /// Local-server configure copy (KTD5 — classification is syntactic; no
    /// network probe or model discovery is performed, R7).
    static let localServerConfigureCaption =
        "Point ScreenCap at an OpenAI-compatible server on this Mac, like Ollama or LM Studio. The URL is classified by its address alone — no request is sent to it."
    static let endpointFieldLabel = "SERVER URL"
    static let endpointFieldPlaceholder = "http://localhost:11434/v1"
    static let endpointSaveButtonTitle = "Save"
    static let endpointClearButtonTitle = "Remove server"
    /// AE3 — the LOCAL classification's stated consequence. (The REMOTE result
    /// reuses `IntelligenceSelectionModel.remoteEndpointNotSelectableCopy` so
    /// the flow and the pane can never state different consequences.)
    static let endpointLocalResultCopy =
        "Local endpoint — runs on this Mac. Answers and day-splitting can use it."

    /// Key-verdict feedback (R7) — the three verdicts render distinct states.
    static let keyVerdictValidCopy = "Key verified and connected."
    static func keyVerdictInvalidCopy(vendorName: String) -> String {
        "That key was rejected by \(vendorName). Nothing was stored."
    }
    static func keyVerdictUnknownCopy(vendorName: String) -> String {
        "Stored, but couldn't reach \(vendorName) to verify it. It'll be used as-is."
    }
    static let disconnectedFeedbackCopy = "Disconnected."

    /// Inline-error fallbacks when the controller carries no message.
    static let keyStoreFailedFallback = "Couldn't store the key."
    static let keyClearFailedFallback = "Couldn't clear the key."
    static let endpointWriteFailedFallback = "Couldn't save the endpoint."
    static let providerSelectFailedFallback = "Couldn't select the provider."

    // MARK: - Honest-copy audit corpus (KTD7)

    /// Every user-facing copy string this model owns — the flow statics, the
    /// per-vendor copy, and every instantiation of the parametrized copy
    /// builders — enumerated for the honest-copy audit
    /// (`testHonestCopyAuditNoForbiddenStrings` scans exactly this list plus
    /// `IntelligenceSelectionModel.allAuditedCopy`). Add every new copy string
    /// here — a string missing from this list escapes the R12 honesty gate.
    static var allAuditedCopy: [String] {
        var out: [String] = [
            flowPickStepTitle, flowPickCaption,
            flowBackButtonTitle, flowDoneButtonTitle,
            vendorChoiceCaption,
            localServerChoiceTitle, localServerChoiceCaption,
            cliAvailabilityHonestCopy,
            localServerConfigureCaption,
            endpointFieldLabel, endpointFieldPlaceholder,
            endpointSaveButtonTitle, endpointClearButtonTitle,
            endpointLocalResultCopy,
            keyVerdictValidCopy, disconnectedFeedbackCopy,
            keyStoreFailedFallback, keyClearFailedFallback,
            endpointWriteFailedFallback, providerSelectFailedFallback,
        ]
        for vendor in vendors {
            out.append(vendor.displayName)
            out.append(vendor.keyBillingCopy)
            out.append(vendor.cliLimitsCopy)
            out.append(vendor.cliFixGuidance)
            out.append(keyVerdictInvalidCopy(vendorName: vendor.displayName))
            out.append(keyVerdictUnknownCopy(vendorName: vendor.displayName))
        }
        for choice in flowChoices {
            out.append(choice.displayName)
            out.append(flowConfigureStepTitle(for: choice))
        }
        for mechanism in BYOMechanism.allCases {
            out.append(mechanism.label)
        }
        return out
    }
}
