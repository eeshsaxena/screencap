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

    /// BYO cloud (U2) — whether a stored API key exists for each vendor. A
    /// *presence flag only*; the CLI never echoes the key value (R3). Drives the
    /// "connected / not connected" state of each BYO-key row in the connect flow.
    let openaiKeyPresent: Bool
    let anthropicKeyPresent: Bool
    let geminiKeyPresent: Bool

    /// BYO cloud (U4) — whether each vendor's delegation CLI is available
    /// (binary resolves AND an auth artifact exists; existence/stat only, KTD1).
    /// Drives the available / needs-attention state of each `*-cli` row (R5/R13).
    let openaiCliAvailable: Bool
    let anthropicCliAvailable: Bool
    let geminiCliAvailable: Bool

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
        case openaiKeyPresent = "openai_key_present"
        case anthropicKeyPresent = "anthropic_key_present"
        case geminiKeyPresent = "gemini_key_present"
        case openaiCliAvailable = "openai_cli_available"
        case anthropicCliAvailable = "anthropic_cli_available"
        case geminiCliAvailable = "gemini_cli_available"
    }

    init(
        provider: String, cloudProvider: String?, summaryCloudConsent: Bool,
        recallCloudConsent: Bool, daySplitCloudConsent: Bool, framesCloudConsent: Bool,
        localServerEndpoint: String? = nil, endpointClassification: String? = nil,
        downloadedModelInstalled: Bool = false,
        openaiKeyPresent: Bool = false, anthropicKeyPresent: Bool = false,
        geminiKeyPresent: Bool = false,
        openaiCliAvailable: Bool = false, anthropicCliAvailable: Bool = false,
        geminiCliAvailable: Bool = false
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
        self.openaiKeyPresent = openaiKeyPresent
        self.anthropicKeyPresent = anthropicKeyPresent
        self.geminiKeyPresent = geminiKeyPresent
        self.openaiCliAvailable = openaiCliAvailable
        self.anthropicCliAvailable = anthropicCliAvailable
        self.geminiCliAvailable = geminiCliAvailable
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
        // BYO fields — `decodeIfPresent` so an older CLI (schema < 3) still decodes.
        openaiKeyPresent = try c.decodeIfPresent(Bool.self, forKey: .openaiKeyPresent) ?? false
        anthropicKeyPresent = try c.decodeIfPresent(Bool.self, forKey: .anthropicKeyPresent) ?? false
        geminiKeyPresent = try c.decodeIfPresent(Bool.self, forKey: .geminiKeyPresent) ?? false
        openaiCliAvailable = try c.decodeIfPresent(Bool.self, forKey: .openaiCliAvailable) ?? false
        anthropicCliAvailable = try c.decodeIfPresent(Bool.self, forKey: .anthropicCliAvailable) ?? false
        geminiCliAvailable = try c.decodeIfPresent(Bool.self, forKey: .geminiCliAvailable) ?? false
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

    // MARK: - BYO cloud writes (U6)

    /// Persist the consented cloud fallback provider (`[intelligence].cloud_provider`),
    /// or clear it with `"none"`. This is the key a BYO provider is selected under
    /// — NOT the active `provider` (KTD2): a BYO id can never be the active/day-split
    /// provider, and the daemon rejects `provider set <byo-id>` outright. Reconciles
    /// against disk so the picker reflects what actually persisted. Returns success
    /// so the pane can surface an inline error.
    @discardableResult
    func setCloudProvider(_ value: String?) async -> Bool {
        let arg = value?.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            _ = try await invoke([
                "settings", "intelligence", "cloud_provider", "set",
                (arg?.isEmpty ?? true) ? "none" : arg!, "--json",
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

    /// Store a BYO API key for `vendor` (`openai` | `anthropic` | `gemini`),
    /// validating it against the vendor first. The secret is piped to the CLI's
    /// STDIN — never an argv element (KTD3) — via `runJSONRawStdin`. Returns a
    /// `BYOKeyResult` carrying the store outcome + validation verdict
    /// (valid / invalid / unknown) so the pane can show store-only-if-valid
    /// feedback; the daemon refuses to store an `invalid` key. On success the
    /// pane should re-select the vendor as `cloud_provider`. Reconciles against
    /// disk so the `*_key_present` flag reflects reality.
    ///
    /// `injectInvoke` is a test seam: the default path shells to the bundled CLI
    /// with the key on stdin; tests substitute a fake that records the argv +
    /// stdin without spawning a process.
    @discardableResult
    func setBYOKey(
        vendor: String,
        key: String,
        validate: Bool = true,
        injectInvoke: (@Sendable ([String], Data) async throws -> Data)? = nil
    ) async -> BYOKeyResult {
        var args = ["settings", "intelligence", "--set-key", vendor]
        if validate { args.append("--validate") }
        args.append("--json")
        let stdinData = Data(key.utf8)
        do {
            let data: Data
            if let injectInvoke {
                data = try await injectInvoke(args, stdinData)
            } else {
                data = try await CLIClient.runJSONRawStdin(args, stdin: stdinData)
            }
            let result = (try? JSONDecoder().decode(BYOKeyResult.self, from: data))
                ?? BYOKeyResult(ok: false, keyPresent: false, validation: nil, error: "decode_failed")
            lastError = result.ok ? nil : (result.error ?? "the key couldn't be stored.")
            await refresh()
            return result
        } catch {
            // A non-zero exit (e.g. an invalid key the daemon rejected) surfaces
            // as a thrown CLIError whose stderr the CLI already redacted of the
            // key; try to recover the JSON envelope the CLI still emits on stdout.
            lastError = error.localizedDescription
            await refresh()
            return BYOKeyResult(
                ok: false, keyPresent: false, validation: nil,
                error: error.localizedDescription
            )
        }
    }

    /// Remove a stored BYO API key for `vendor`. If that vendor's key-based id is
    /// the currently-selected `cloud_provider`, the caller should also clear the
    /// selection. Reconciles against disk. Returns success for inline errors.
    @discardableResult
    func clearBYOKey(vendor: String) async -> Bool {
        do {
            _ = try await invoke([
                "settings", "intelligence", "--clear-key", vendor, "--json",
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
}

/// The decoded result of a BYO `--set-key` write. `validation` is the vendor's
/// verdict: `"valid"` (confirmed), `"invalid"` (rejected — not stored), or
/// `"unknown"` (couldn't reach the vendor — stored anyway, surfaced as unverified),
/// or nil when validation was skipped.
struct BYOKeyResult: Decodable, Equatable {
    let ok: Bool
    let keyPresent: Bool
    let validation: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case keyPresent = "key_present"
        case validation
        case error
    }

    init(ok: Bool, keyPresent: Bool, validation: String?, error: String?) {
        self.ok = ok
        self.keyPresent = keyPresent
        self.validation = validation
        self.error = error
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        keyPresent = try c.decodeIfPresent(Bool.self, forKey: .keyPresent) ?? false
        validation = try c.decodeIfPresent(String.self, forKey: .validation)
        error = try c.decodeIfPresent(String.self, forKey: .error)
    }

    /// Verdict constants mirroring `screencap.segmentation.secrets`.
    static let valid = "valid"
    static let invalid = "invalid"
    static let unknown = "unknown"
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
            downloadedModelInstalled: downloadedModelInstalled,
            openaiKeyPresent: openaiKeyPresent,
            anthropicKeyPresent: anthropicKeyPresent,
            geminiKeyPresent: geminiKeyPresent,
            openaiCliAvailable: openaiCliAvailable,
            anthropicCliAvailable: anthropicCliAvailable,
            geminiCliAvailable: geminiCliAvailable
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
            downloadedModelInstalled: downloadedModelInstalled,
            openaiKeyPresent: openaiKeyPresent,
            anthropicKeyPresent: anthropicKeyPresent,
            geminiKeyPresent: geminiKeyPresent,
            openaiCliAvailable: openaiCliAvailable,
            anthropicCliAvailable: anthropicCliAvailable,
            geminiCliAvailable: geminiCliAvailable
        )
    }

    /// Convenience — the presence flag for a BYO-key vendor id
    /// (`openai`/`anthropic`/`gemini`), or false for an unknown id.
    func keyPresent(forVendor vendor: String) -> Bool {
        switch vendor {
        case "openai": return openaiKeyPresent
        case "anthropic": return anthropicKeyPresent
        case "gemini": return geminiKeyPresent
        default: return false
        }
    }

    /// Convenience — the availability flag for a BYO CLI id
    /// (`openai-cli`/`anthropic-cli`/`gemini-cli`), or false for an unknown id.
    func cliAvailable(forProviderID id: String) -> Bool {
        switch id {
        case "openai-cli": return openaiCliAvailable
        case "anthropic-cli": return anthropicCliAvailable
        case "gemini-cli": return geminiCliAvailable
        default: return false
        }
    }
}
