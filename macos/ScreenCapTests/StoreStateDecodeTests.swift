import XCTest
@testable import ScreenCap

/// SCR-258 U10 (KTD-20, AE8): the two shapes `screencap list --json` now emits and
/// the tolerant `store_state` decode. The CLI-fallback path (`StoreListPayload`) is
/// the crux — a non-mounted store must decode to a first-class state, never a
/// decode error that the Library treats as a generic load failure.
final class StoreStateDecodeTests: XCTestCase {

    // MARK: - Bare array (mounted / plaintext, back-compat)

    func testBareArrayDecodesAsMounted() throws {
        let json = """
        [
          {
            "name": "rec-1", "date": "2026-07-03", "duration": "0:42", "size_mb": "1 MB",
            "has_audio": false, "transcribed": false, "uploaded": false, "is_stub": false,
            "chunks_total": 0, "chunks_uploaded": 0, "intent": null,
            "started_at": 1751536800.0, "duration_seconds": 42.0, "drops": null
          }
        ]
        """
        let payload = try StoreListPayload.decode(Data(json.utf8))
        XCTAssertEqual(payload.storeState, .mounted)
        XCTAssertEqual(payload.recordings.count, 1)
        XCTAssertEqual(payload.recordings.first?.name, "rec-1")
    }

    func testEmptyBareArrayIsMountedAndEmpty() throws {
        let payload = try StoreListPayload.decode(Data("[]".utf8))
        XCTAssertEqual(payload.storeState, .mounted)
        XCTAssertTrue(payload.recordings.isEmpty)
    }

    // MARK: - Typed object (non-mounted)

    func testLockedEnvelopeDecodesAsLockedWithNoRows() throws {
        let json = #"{"store_state": "locked", "recordings": []}"#
        let payload = try StoreListPayload.decode(Data(json.utf8))
        XCTAssertEqual(payload.storeState, .locked)
        XCTAssertTrue(payload.recordings.isEmpty,
                      "a locked store carries an empty list, not a decode error")
    }

    func testAbsentEnvelopeDecodesAsAbsent() throws {
        let json = #"{"store_state": "absent", "recordings": []}"#
        let payload = try StoreListPayload.decode(Data(json.utf8))
        XCTAssertEqual(payload.storeState, .absent)
    }

    func testErrorEnvelopeWithReasonDecodesReason() throws {
        // list --json does not emit store_reason today, but StoreListPayload tolerates
        // it if present so an enriched future payload still resolves the reason.
        let json = #"{"store_state": "error", "store_reason": "key_missing", "recordings": []}"#
        let payload = try StoreListPayload.decode(Data(json.utf8))
        XCTAssertEqual(payload.storeState, .error(reason: .keyMissing))
    }

    func testErrorEnvelopeWithoutReasonIsUnknown() throws {
        let json = #"{"store_state": "error", "recordings": []}"#
        let payload = try StoreListPayload.decode(Data(json.utf8))
        XCTAssertEqual(payload.storeState, .error(reason: .unknown))
    }

    func testMalformedPayloadThrows() {
        XCTAssertThrowsError(try StoreListPayload.decode(Data("not json".utf8)),
                             "a genuinely malformed payload must surface as a real error")
    }

    // MARK: - StoreState.from tolerance

    func testUnknownStateTokenIsMounted() {
        XCTAssertEqual(StoreState.from(state: "some_future_state"), .mounted)
        XCTAssertEqual(StoreState.from(state: nil), .mounted)
        XCTAssertEqual(StoreState.from(state: "mounted"), .mounted)
    }

    func testAllErrorReasonsMap() {
        XCTAssertEqual(StoreState.from(state: "error", reason: "key_missing"),
                       .error(reason: .keyMissing))
        XCTAssertEqual(StoreState.from(state: "error", reason: "entitlement_mismatch"),
                       .error(reason: .entitlementMismatch))
        XCTAssertEqual(StoreState.from(state: "error", reason: "keychain_locked"),
                       .error(reason: .keychainLocked))
        XCTAssertEqual(StoreState.from(state: "error", reason: "downgrade_unsupported"),
                       .error(reason: .downgradeUnsupported))
        XCTAssertEqual(StoreState.from(state: "error", reason: "future_reason"),
                       .error(reason: .unknown))
    }

    // MARK: - Response envelope decode (daemon path)

    func testListResponseCarriesStoreState() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.11.0",
          "api_schema_version": 1, "recordings": [], "store_state": "locked"
        }
        """
        let resp = try JSONDecoder().decode(ListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.resolvedStoreState, .locked)
    }

    func testListResponseWithoutStoreStateDefaultsMounted() throws {
        // An older daemon omits store_state → mounted (never blanks the library).
        let json = """
        {"ok": true, "schema_version": 1, "daemon_version": "0.10.0",
         "api_schema_version": 1, "recordings": []}
        """
        let resp = try JSONDecoder().decode(ListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.resolvedStoreState, .mounted)
    }

    func testDaemonInfoCarriesStoreStateAndReason() throws {
        let json = """
        {
          "ok": true, "schema_version": 1, "daemon_version": "0.11.0",
          "api_schema_version": 1, "started_at": 0.0,
          "store_state": "error", "store_reason": "entitlement_mismatch"
        }
        """
        let resp = try JSONDecoder().decode(DaemonInfoResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.resolvedStoreState, .error(reason: .entitlementMismatch))
    }
}
