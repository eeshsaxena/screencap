import CryptoKit
import Foundation
import Security

// Search U6 (OQ4 = option a): the Swift read-side decrypt for the encrypted recall
// corpus. The app reads the corpus key directly from the shared Keychain access
// group (`2A8S6MV8DZ.com.screencap.shared` — the SCR-241 group the daemon + CLI
// already use) and decrypts stills in-memory with CryptoKit, so a search-result /
// truth-pane thumbnail decodes without a daemon round-trip.
//
// The wire format is byte-identical to Python `screencap.corpus_crypto`:
//   token = MAGIC("SCE1") ‖ nonce(12) ‖ ciphertext ‖ GCM-tag(16)
// and the AES-256-GCM AAD is the canonical JSON `{"n":<name>,"r":<recording>}`
// (sorted keys, no spaces, `ensure_ascii`) produced by `corpus_crypto.corpus_aad`.
// Any drift here silently fails every decrypt, so both sides pin this format.

enum CorpusCryptoError: Error {
    case keyUnavailable
    case badFormat
    case decryptFailed
}

struct CorpusCrypto {
    static let magic = Data("SCE1".utf8)
    static let nonceLength = 12
    static let tagLength = 16
    static let keyLength = 32

    static let keychainService = "screencap-corpus"
    static let keychainAccount = "key"
    static let keychainAccessGroup = "2A8S6MV8DZ.com.screencap.shared"

    let key: SymmetricKey

    /// Build from an explicit key (test seam), or load from the shared Keychain group.
    init(key: SymmetricKey) { self.key = key }

    init(loadingFromKeychain: Bool = true) throws {
        guard loadingFromKeychain, let loaded = CorpusCrypto.loadKey() else {
            throw CorpusCryptoError.keyUnavailable
        }
        self.key = loaded
    }

    /// Read the 32-byte corpus key from the shared access group, or nil if absent.
    ///
    /// The stored value is the UTF-8 bytes of a base64 string (Python writes
    /// `base64.b64encode(key).decode()` through `keychain_group.store`), so we
    /// decode UTF-8 → base64 → 32 raw bytes.
    static func loadKey(
        service: String = keychainService,
        account: String = keychainAccount,
        accessGroup: String = keychainAccessGroup
    ) -> SymmetricKey? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecUseDataProtectionKeychain as String: true,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let base64 = String(data: data, encoding: .utf8),
              let raw = Data(base64Encoded: base64),
              raw.count == keyLength
        else { return nil }
        return SymmetricKey(data: raw)
    }

    /// The AAD for a `(recording, name)` — must byte-match `corpus_crypto.corpus_aad`.
    static func aad(recording: String, name: String) -> Data {
        Data("{\"n\":\(jsonString(name)),\"r\":\(jsonString(recording))}".utf8)
    }

    /// Minimal JSON string encoder matching Python `json.dumps(..., ensure_ascii=True)`
    /// for the ASCII recording/frame identifiers this corpus uses.
    static func jsonString(_ value: String) -> String {
        var out = "\""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if scalar.value < 0x20 || scalar.value > 0x7E {
                    out += String(format: "\\u%04x", scalar.value)
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        out += "\""
        return out
    }

    /// Decrypt a corpus token into its plaintext bytes.
    func decrypt(_ token: Data, recording: String, name: String) throws -> Data {
        let header = Self.magic.count + Self.nonceLength
        guard token.count >= header + Self.tagLength,
              token.prefix(Self.magic.count) == Self.magic
        else { throw CorpusCryptoError.badFormat }

        let nonceData = token.subdata(in: token.startIndex + Self.magic.count ..< token.startIndex + header)
        let body = token.subdata(in: token.startIndex + header ..< token.endIndex)
        let ciphertext = body.prefix(body.count - Self.tagLength)
        let tag = body.suffix(Self.tagLength)
        do {
            let box = try AES.GCM.SealedBox(
                nonce: AES.GCM.Nonce(data: nonceData),
                ciphertext: ciphertext,
                tag: tag
            )
            return try AES.GCM.open(box, using: key, authenticating: Self.aad(recording: recording, name: name))
        } catch {
            throw CorpusCryptoError.decryptFailed
        }
    }

    /// Decrypt an encrypted still at a `.../<recording>/screenshots/<ts>.jpg.enc` URL,
    /// deriving the AAD `(recording, name)` from the path exactly as the writer did.
    func decryptStill(at url: URL) throws -> Data {
        let token = try Data(contentsOf: url)
        let recording = url.deletingLastPathComponent().deletingLastPathComponent().lastPathComponent
        let name = CorpusCrypto.logicalStillName(url)
        return try decrypt(token, recording: recording, name: name)
    }

    /// The logical (plaintext) basename of a still — `<ts>.jpg`, `.enc` stripped.
    static func logicalStillName(_ url: URL) -> String {
        let name = url.lastPathComponent
        return name.hasSuffix(".enc") ? String(name.dropLast(4)) : name
    }

    /// Whether a URL names an encrypted still (`*.jpg.enc`).
    static func isEncryptedStill(_ url: URL) -> Bool {
        url.lastPathComponent.hasSuffix(".jpg.enc")
    }
}
