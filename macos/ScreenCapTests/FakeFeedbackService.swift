import Foundation
@testable import ScreenCap

/// Fake `FeedbackService` (U3): feeds a canned envelope (or error) to
/// `FeedbackController` and records every payload + timeout it was asked to
/// send, so tests can assert the stdin contract and the KTD-12 scaled timeout
/// without spawning a real subprocess.
@MainActor
final class FakeFeedbackService: FeedbackService {
    /// The stdout bytes the next send resolves with.
    var result = Data()
    /// When set, the next send throws instead of returning `result`.
    var error: Error?
    /// When true, the send suspends until the awaiting task is cancelled —
    /// the seam for exercising `.sending` and `cancelSend()`.
    var suspendUntilCancelled = false

    private(set) var sentPayloads: [Data] = []
    private(set) var sentTimeouts: [TimeInterval] = []

    func send(payload: Data, timeout: TimeInterval) async throws -> Data {
        sentPayloads.append(payload)
        sentTimeouts.append(timeout)
        if suspendUntilCancelled {
            // Far beyond any test's patience; only task cancellation ends it.
            try await Task.sleep(nanoseconds: 3_600 * 1_000_000_000)
        }
        if let error { throw error }
        return result
    }

    /// The last payload decoded as a JSON object, for stdin-contract asserts.
    var lastPayloadJSON: [String: Any] {
        guard let data = sentPayloads.last,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return object
    }

    static func envelope(_ json: String) -> Data { Data(json.utf8) }
}
