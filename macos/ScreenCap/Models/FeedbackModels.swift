import Foundation

/// The three request types the feedback relay routes to Linear labels
/// (feat/in-app-feedback-form U3). Raw values are the cross-language contract
/// with `screencap feedback send` and the relay's `REQUEST_TYPES` map — they
/// must match `scripts/cloud-function/feedback.py` exactly.
enum FeedbackRequestType: String, CaseIterable, Identifiable {
    case bug
    case feedback
    case feature

    var id: String { rawValue }

    /// Segmented-picker label.
    var label: String {
        switch self {
        case .bug: return "Bug report"
        case .feedback: return "Feedback"
        case .feature: return "Feature request"
        }
    }
}

/// Attachment caps and accepted types (KTD-5). MIRRORED constants — kept equal
/// to `src/screencap/feedback.py` and `scripts/cloud-function/feedback.py` by
/// hand (no import mechanism spans the three runtimes; the CLI↔relay pair is
/// pinned equal by pytest). Silent drift here reproduces the submit-time cap
/// surprise R5 forbids, so change all three together.
enum FeedbackCaps {
    static let maxFileBytes = 25 * 1024 * 1024
    static let maxTotalBytes = 60 * 1024 * 1024
    static let maxAttachments = 5
    /// Relay-side `MAX_MESSAGE_CHARS` — enforced here so the user learns while
    /// typing, not from a round-trip `invalid`.
    static let maxMessageChars = 10_000

    /// Lowercased path extension → the content type the CLI/relay accept.
    /// Anything absent from this map is rejected at selection time (AE7).
    static let contentTypeByExtension: [String: String] = [
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "heic": "image/heic",
        "mp4": "video/mp4",
        "mov": "video/quicktime",
    ]
}

/// One user-chosen attachment in the draft. `sizeBytes` is captured at
/// selection so cap math and the payload-scaled timeout never re-stat the file.
struct FeedbackAttachment: Identifiable, Equatable {
    let id: UUID
    let path: String
    let fileName: String
    let sizeBytes: Int
    let contentType: String
}

/// Why a candidate file was refused at selection time (AE2/AE6/AE7). Each case
/// carries distinct copy so the view never renders a generic "can't attach".
enum FeedbackAttachmentRejection: Equatable {
    case unsupportedType(fileName: String)
    case fileTooLarge(fileName: String)
    case totalTooLarge(fileName: String)
    case tooMany
    case unreadable(fileName: String)

    var message: String {
        switch self {
        case .unsupportedType(let name):
            return "“\(name)” isn't a supported type. Attach a screenshot (PNG, JPEG, GIF, HEIC) or a short clip (MP4, MOV)."
        case .fileTooLarge(let name):
            return "“\(name)” is over the 25 MB per-file limit. Try a shorter clip — around 30 seconds works best."
        case .totalTooLarge(let name):
            return "Adding “\(name)” would push the attachments past the 60 MB total limit."
        case .tooMany:
            return "You can attach up to \(FeedbackCaps.maxAttachments) files per report."
        case .unreadable(let name):
            return "“\(name)” couldn't be read. Pick the file again."
        }
    }
}

/// Selection-time validation as pure functions (U3), so the cap/type rules are
/// unit-testable without touching the filesystem.
enum FeedbackAttachmentPolicy {
    static func contentType(forExtension ext: String) -> String? {
        FeedbackCaps.contentTypeByExtension[ext.lowercased()]
    }

    /// The first rule a candidate breaks, or nil when it can join the draft.
    /// Order matters for honest copy: an unsupported `.log` should say "type",
    /// not "too large", even if it is both.
    static func rejection(
        fileName: String,
        fileExtension: String,
        sizeBytes: Int,
        existingCount: Int,
        existingTotalBytes: Int
    ) -> FeedbackAttachmentRejection? {
        if contentType(forExtension: fileExtension) == nil {
            return .unsupportedType(fileName: fileName)
        }
        if existingCount >= FeedbackCaps.maxAttachments {
            return .tooMany
        }
        if sizeBytes > FeedbackCaps.maxFileBytes {
            return .fileTooLarge(fileName: fileName)
        }
        if existingTotalBytes + sizeBytes > FeedbackCaps.maxTotalBytes {
            return .totalTooLarge(fileName: fileName)
        }
        return nil
    }
}

/// The typed error taxonomy the CLI envelope carries (KTD-7). Every kind maps
/// to distinct copy naming the concrete next step — R9 forbids one generic
/// "couldn't send". An unrecognized future kind resolves to `.server` (the
/// honest "something went wrong, retry" fallback) rather than crashing or
/// leaking envelope text.
enum FeedbackErrorKind: Equatable, CaseIterable {
    case network
    case server
    case rateLimited
    case tooLarge
    case invalid
    case expired

    static func from(code: String?) -> FeedbackErrorKind {
        switch code {
        case "network": return .network
        case "rate_limited": return .rateLimited
        case "too_large": return .tooLarge
        case "invalid": return .invalid
        case "expired": return .expired
        default: return .server
        }
    }

    /// Fallback when the envelope omits an explicit `retryable` (mirrors the
    /// CLI's `_RETRYABLE_KINDS`).
    var defaultRetryable: Bool {
        switch self {
        case .network, .server, .expired: return true
        case .rateLimited, .tooLarge, .invalid: return false
        }
    }

    /// Static user-facing copy — never envelope text (the R10-style leak the
    /// account error seam also closes).
    var userMessage: String {
        switch self {
        case .network:
            return "You appear to be offline. Check your connection and try again."
        case .server:
            return "Something went wrong sending your report. Try again."
        case .rateLimited:
            return "Too many reports from this connection right now. Try again in an hour."
        case .tooLarge:
            return "The attachments are over the size limit. Remove one and try again."
        case .invalid:
            return "The report couldn't be prepared. Check the form and try again."
        case .expired:
            return "The upload took too long and timed out. Try sending again."
        }
    }
}

/// A settled send failure: the kind, its static copy, whether Retry is honest,
/// and — for `invalid` only — the relay's short field reason (KTD-7 surfaces
/// it as secondary detail; it originates from our own relay/CLI, never from
/// user-controlled input).
struct FeedbackFailure: Equatable {
    let kind: FeedbackErrorKind
    let message: String
    let retryable: Bool
    let detail: String?
}

/// Submission lifecycle the sheet renders (U3), mirroring
/// `ClipExportState`'s state-enum pattern.
enum FeedbackSendState: Equatable {
    case idle
    case sending
    case success(issueURL: String?)
    case failure(FeedbackFailure)
}

/// Decoded `screencap feedback send` stdout envelope. Tolerant contract like
/// `AuthWhoAmIEnvelope`: every field optional, success keyed on `ok` alone —
/// never gate on nullable fields (`issue_url` may be absent).
struct FeedbackSendEnvelope: Decodable, Equatable {
    let ok: Bool?
    let errorKind: String?
    let message: String?
    let retryable: Bool?
    let issueUrl: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case errorKind = "error_kind"
        case message
        case retryable
        case issueUrl = "issue_url"
    }

    static func parse(_ data: Data) -> FeedbackSendEnvelope? {
        guard !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(FeedbackSendEnvelope.self, from: data)
    }
}

/// The stdin payload for `screencap feedback send` (U2 contract):
/// `{type, message, email, versions{app,daemon,macos}, attachments:[{path,
/// content_type}]}`.
struct FeedbackPayload: Encodable, Equatable {
    struct Versions: Encodable, Equatable {
        let app: String
        let daemon: String
        let macos: String
    }

    struct Attachment: Encodable, Equatable {
        let path: String
        let contentType: String

        enum CodingKeys: String, CodingKey {
            case path
            case contentType = "content_type"
        }
    }

    let type: String
    let message: String
    let email: String
    let versions: Versions
    let attachments: [Attachment]
}

/// Pure form-derivation rules (U4's view logic, tested in U3's file per the
/// `AccountSheetPolicy` precedent).
enum FeedbackFormPolicy {
    /// Deliberately loose — the relay owns strict validation; this only stops
    /// obvious typos ("foo") from sending an unreachable reply address. Empty
    /// is always valid (anonymous submission, AE5).
    static func isEmailAcceptable(_ email: String) -> Bool {
        let trimmed = email.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return true }
        let parts = trimmed.split(separator: "@")
        return parts.count == 2 && parts[1].contains(".") && !trimmed.contains(" ")
    }

    /// Send-button enablement: a non-empty message, an acceptable email, a
    /// message under the relay cap, and no send already in flight.
    static func canSend(message: String, email: String, isSending: Bool) -> Bool {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmed.isEmpty
            && message.count <= FeedbackCaps.maxMessageChars
            && isEmailAcceptable(email)
            && !isSending
    }
}

/// KTD-12: the wall-clock subprocess timeout, scaled to declared attachment
/// bytes. `runJSONRawStdin`'s 10s default would SIGTERM every attachment-bearing
/// submission mid-PUT; this must exceed worst-case upload time on a slow home
/// uplink (~60s base + ~15s per declared MB, floor 120s, cap 10 min).
enum FeedbackSendTimeout {
    static func seconds(declaredBytes: Int) -> TimeInterval {
        let megabytes = Double(declaredBytes) / (1024 * 1024)
        return min(600, max(120, 60 + 15 * megabytes))
    }
}
