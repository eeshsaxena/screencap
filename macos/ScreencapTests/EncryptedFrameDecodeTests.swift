import CryptoKit
import ImageIO
import XCTest
@testable import Screencap

/// Search U6: the app's read path for the encrypted corpus — `RecordingFrameIndex`
/// enumerates `*.jpg.enc` alongside `*.jpg`, and the `ThumbnailLoader` decrypting
/// decode turns an encrypted still into a `CGImage`. Uses a token produced by the
/// same format as Python `corpus_crypto` (round-tripped through the Swift decrypt),
/// so no real Keychain is involved.
final class EncryptedFrameDecodeTests: XCTestCase {

    private let key = SymmetricKey(data: Data((0..<32).map { UInt8($0) }))

    /// Build an encrypted-still token (MAGIC ‖ nonce ‖ ct ‖ tag) for a JPEG, bound to
    /// the given `(recording, name)` — matches the on-disk `.jpg.enc` writer.
    private func encryptedStill(_ jpeg: Data, recording: String, name: String) throws -> Data {
        let nonce = AES.GCM.Nonce()
        let sealed = try AES.GCM.seal(
            jpeg, using: key, nonce: nonce,
            authenticating: CorpusCrypto.aad(recording: recording, name: name)
        )
        return CorpusCrypto.magic + Data(nonce) + sealed.ciphertext + sealed.tag
    }

    private func tinyJPEG() -> Data {
        let cs = CGColorSpace(name: CGColorSpace.sRGB)!
        let ctx = CGContext(data: nil, width: 8, height: 8, bitsPerComponent: 8, bytesPerRow: 0,
                            space: cs, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: 8, height: 8))
        let cg = ctx.makeImage()!
        let out = NSMutableData()
        let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil)!
        CGImageDestinationAddImage(dest, cg, nil)
        CGImageDestinationFinalize(dest)
        return out as Data
    }

    func testFrameIndexEnumeratesEncryptedAndDedupes() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("frames-\(UUID().uuidString)", isDirectory: true)
        let ss = root.appendingPathComponent("rec-1/screenshots", isDirectory: true)
        try FileManager.default.createDirectory(at: ss, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        try Data("x".utf8).write(to: ss.appendingPathComponent("100.000000.jpg"))
        try Data("y".utf8).write(to: ss.appendingPathComponent("200.000000.jpg.enc"))
        // Same frame present as both forms mid-migration → listed once.
        try Data("a".utf8).write(to: ss.appendingPathComponent("300.000000.jpg"))
        try Data("b".utf8).write(to: ss.appendingPathComponent("300.000000.jpg.enc"))

        let frames = RecordingFrameIndex.loadFrames(root: root, recording: "rec-1")
        XCTAssertEqual(frames.map(\.ms), [100_000, 200_000, 300_000])
    }

    func testDecryptingDecodeDecodesEncryptedStill() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("dec-\(UUID().uuidString)", isDirectory: true)
        let ss = root.appendingPathComponent("rec-1/screenshots", isDirectory: true)
        try FileManager.default.createDirectory(at: ss, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let name = "150.000000.jpg"
        let url = ss.appendingPathComponent(name + ".enc")
        try encryptedStill(tinyJPEG(), recording: "rec-1", name: name).write(to: url)

        let decode = ThumbnailLoader.makeDecryptingDecode(corpus: CorpusCrypto(key: key))
        XCTAssertNotNil(decode(url, 160), "encrypted still should decrypt + decode")

        // Without a key, an encrypted still declines to a nil (→ placeholder).
        let noKey = ThumbnailLoader.makeDecryptingDecode(corpus: nil)
        XCTAssertNil(noKey(url, 160))
    }
}
