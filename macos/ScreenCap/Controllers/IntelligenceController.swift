import Combine
import Foundation

/// The `intelligence` block emitted by `screencap settings intelligence --json`
/// (U8). Drives the Intelligence settings pane (U9): the model picker (active
/// provider + configured cloud provider) and the per-task cloud-consent matrix.
///
/// The two fixed guards — `daySplitCloudConsent` (R7) and `framesCloudConsent`
/// (R9) — are surfaced by the CLI as constant `false` so the pane renders them
/// as non-interactive "on-device"/"always off" rows without hard-coding the
/// rule. They are decoded here for symmetry and asserted-fixed in tests, but
/// never written (the CLI rejects a cloud write to either — defense in depth).
struct IntelligenceSettings: Decodable, Equatable {
    /// The active provider — `on-device` (default) or a configured cloud
    /// provider id (e.g. `gemini`).
    let provider: String
    /// The configured cloud fallback provider, or nil when none is set.
    let cloudProvider: String?
    /// R8 — summaries/titles may use cloud (transcript text only) when on.
    let summaryCloudConsent: Bool
    /// R10 — recall-answering may use cloud when on.
    let recallCloudConsent: Bool
    /// R7 — day-splitting/labeling stays on-device; always false.
    let daySplitCloudConsent: Bool
    /// R9 — screen frames/images are never sent to any cloud; always false.
    let framesCloudConsent: Bool
    /// SCR-239 — the configured bring-your-own endpoint (redacted; no token), or
    /// nil. `decodeIfPresent` so an older CLI that omits it still decodes.
    let localServerEndpoint: String?
    /// SCR-239 — the endpoint's LOCAL/REMOTE classification, or nil when unset.
    let endpointClassification: String?
    /// SCR-239 — whether the downloadable model is installed on this Mac.
    let downloadedModelInstalled: Bool

    enum CodingKeys: String, CodingKey {
        case provider
        case cloudProvider = "cloud_provider"
        case summaryCloudConsent = "summary_cloud_consent"
        case recallCloudConsent = "recall_cloud_consent"
        case daySplitCloudConsent = "day_split_cloud_consent"
        case framesCloudConsent = "frames_cloud_consent"
        case localServerEndpoint = "local_server_endpoint"
        case endpointClassification = "endpoint_classification"
        case downloadedModelInstalled = "downloaded_model_installed"
    }

    init(
        provider: String, cloudProvider: String?, summaryCloudConsent: Bool,
        recallCloudConsent: Bool, daySplitCloudConsent: Bool, framesCloudConsent: Bool,
        localServerEndpoint: String? = nil, endpointClassification: String? = nil,
        downloadedModelInstalled: Bool = false
    ) {
        self.provider = provider
        self.cloudProvider = cloudProvider
        self.summaryCloudConsent = summaryCloudConsent
        self.recallCloudConsent = recallCloudConsent
        self.daySplitCloudConsent = daySplitCloudConsent
        self.framesCloudConsent = framesCloudConsent
        self.localServerEndpoint = localServerEndpoint
        self.endpointClassification = endpointClassification
        self.downloadedModelInstalled = downloadedModelInstalled
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = try c.decode(String.self, forKey: .provider)
        cloudProvider = try c.decodeIfPresent(String.self, forKey: .cloudProvider)
        summaryCloudConsent = try c.decode(Bool.self, forKey: .summaryCloudConsent)
        recallCloudConsent = try c.decode(Bool.self, forKey: .recallCloudConsent)
        daySplitCloudConsent = try c.decode(Bool.self, forKey: .daySplitCloudConsent)
        framesCloudConsent = try c.decode(Bool.self, forKey: .framesCloudConsent)
        localServerEndpoint = try c.decodeIfPresent(String.self, forKey: .localServerEndpoint)
        endpointClassification = try c.decodeIfPresent(String.self, forKey: .endpointClassification)
        downloadedModelInstalled =
            try c.decodeIfPresent(Bool.self, forKey: .downloadedModelInstalled) ?? false
    }
}

/// Outer envelope for `screencap settings intelligence --json` (read-back).
struct IntelligenceEnvelope: Decodable {
    let ok: Bool
    let schemaVersion: Int?
    let intelligence: IntelligenceSettings

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case intelligence
    }
}

/// Owner of Intelligence-settings state for the Intelligence pane (U9). Mirrors
/// `PrivacyController`: reads the `intelligence` block via `screencap settings
/// intelligence --json` and writes through `screencap settings intelligence
/// <row> set <value>`. No Keychain — cloud provider keys stay daemon-owned
/// (R8/R9 trust boundary); this controller only moves pointers + consent bools.
@MainActor
final class IntelligenceController: ObservableObject {
    /// The last-read settings block. nil until the first `refresh()` (or on an
    /// older CLI that omits the intelligence verb) — the pane renders a loading
    /// state rather than guessing at a default.
    @Published private(set) var settings: IntelligenceSettings?
    @Published private(set) var lastError: String?

    /// Pluggable CLI invoker — the same seam `PrivacyController` uses, so tests
    /// can assert on exact argv vectors (the contract that ships to Python)
    /// without touching Foundation pipe machinery.
    typealias JSONInvoker = @Sendable ([String]) async throws -> Data
    private let invoke: JSONInvoker

    /// Serializes a single consent-row write in flight — a double-tap on a
    /// toggle mid-round-trip would otherwise race two writes and leave the
    /// optimistic value pointing at whichever landed last. Keyed by row so
    /// independent rows (summary vs recall) never block each other.
    private var pendingRows: Set<String> = []

    /// Serializes provider-picker writes for the same reason.
    private var providerWriteInFlight: Bool = false

    init(invoke: @escaping JSONInvoker = IntelligenceController.defaultInvoke) {
        self.invoke = invoke
    }

    /// Default invoker — talks to the bundled `screencap` binary via `CLIClient`.
    static let defaultInvoke: JSONInvoker = { args in
        try await CLIClient.runJSONRaw(args)
    }

    // MARK: - Read

    /// Re-fetch the `intelligence` block. A failure surfaces in `lastError` and
    /// leaves `settings` unchanged so a transient CLI hiccup doesn't blank the
    /// pane the user is looking at.
    func refresh() async {
        do {
            let data = try await invoke(["settings", "intelligence", "--json"])
            let envelope = try JSONDecoder().decode(IntelligenceEnvelope.self, from: data)
            guard envelope.ok else {
                lastError = "settings intelligence --json reported ok=false"
                return
            }
            settings = envelope.intelligence
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    // MARK: - Writes

    /// Select the active provider (`on-device` or a cloud provider id). Optimistic
    /// flip + revert-on-failure: the published provider flips immediately so the
    /// picker tracks the tap; a CLI failure restores the previous value and
    /// surfaces the error, so the picker never rests contradicting disk. Returns
    /// success so the pane can show an inline error.
    @discardableResult
    func setProvider(_ value: String) async -> Bool {
        guard !providerWriteInFlight else { return false }
        providerWriteInFlight = true
        defer { providerWriteInFlight = false }

        let previous = settings
        if let current = settings {
            settings = current.with(provider: value)
        }
        do {
            _ = try await invoke(["settings", "intelligence", "provider", "set", value, "--json"])
            lastError = nil
            // Reconcile against disk — provider selection may materialize a
            // cloud_provider the picker should reflect.
            await refresh()
            return true
        } catch {
            settings = previous
            lastError = error.localizedDescription
            return false
        }
    }

    /// Set (or clear with an empty string) the bring-your-own endpoint URL
    /// (SCR-239). Writes `local_server_endpoint set <url>` then reconciles against
    /// disk so the pane reflects the resolved LOCAL/REMOTE classification. Returns
    /// success so the field can show an inline error.
    @discardableResult
    func setEndpoint(_ value: String) async -> Bool {
        let arg = value.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            _ = try await invoke([
                "settings", "intelligence", "local_server_endpoint", "set",
                arg.isEmpty ? "none" : arg, "--json",
            ])
            lastError = nil
            await refresh()
            return true
        } catch {
            lastError = error.localizedDescription
            await refresh()
            return false
        }
    }

    /// Toggle a cloud-consent row (`summary_cloud_consent` / `recall_cloud_consent`).
    /// Optimistic flip + revert-on-failure, serialized per row. The forbidden rows
    /// (day-split, frames) are never written from here — the pane renders them as
    /// non-interactive and the CLI hard-rejects a cloud write to either anyway.
    /// Returns success so the pane can show an inline error and snap the toggle back.
    @discardableResult
    func setConsent(row: String, enabled: Bool) async -> Bool {
        guard !pendingRows.contains(row) else { return false }
        pendingRows.insert(row)
        defer { pendingRows.remove(row) }

        let previous = settings
        if let current = settings {
            settings = current.with(consentRow: row, enabled: enabled)
        }
        do {
            _ = try await invoke(
                ["settings", "intelligence", row, "set", enabled ? "true" : "false", "--json"]
            )
            lastError = nil
            return true
        } catch {
            settings = previous
            lastError = error.localizedDescription
            return false
        }
    }
}

extension IntelligenceSettings {
    /// A copy with the active provider replaced (optimistic picker flip).
    func with(provider: String) -> IntelligenceSettings {
        IntelligenceSettings(
            provider: provider,
            cloudProvider: cloudProvider,
            summaryCloudConsent: summaryCloudConsent,
            recallCloudConsent: recallCloudConsent,
            daySplitCloudConsent: daySplitCloudConsent,
            framesCloudConsent: framesCloudConsent,
            localServerEndpoint: localServerEndpoint,
            endpointClassification: endpointClassification,
            downloadedModelInstalled: downloadedModelInstalled
        )
    }

    /// A copy with one consent row replaced (optimistic toggle flip). Only the
    /// two settable rows are handled; the fixed rows are never mutated here.
    func with(consentRow row: String, enabled: Bool) -> IntelligenceSettings {
        IntelligenceSettings(
            provider: provider,
            cloudProvider: cloudProvider,
            summaryCloudConsent: row == "summary_cloud_consent" ? enabled : summaryCloudConsent,
            recallCloudConsent: row == "recall_cloud_consent" ? enabled : recallCloudConsent,
            daySplitCloudConsent: daySplitCloudConsent,
            framesCloudConsent: framesCloudConsent,
            localServerEndpoint: localServerEndpoint,
            endpointClassification: endpointClassification,
            downloadedModelInstalled: downloadedModelInstalled
        )
    }
}
