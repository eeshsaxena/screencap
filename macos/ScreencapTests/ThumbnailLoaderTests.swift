import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import Screencap

// SCR-177 U2 — cache hit avoids re-decode, concurrent requests for one URL
// coalesce to a single decode (the fork-bomb guard), real downsampling bounds
// the output dimensions, and a missing file resolves to nil (placeholder, R5).
final class ThumbnailLoaderTests: XCTestCase {

    /// Thread-safe call counter (the injected decode may run off the main actor).
    private final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func increment() { lock.lock(); value += 1; lock.unlock() }
        var count: Int { lock.lock(); defer { lock.unlock() }; return value }
    }

    private static func dummyThumbnail() -> ThumbnailImage {
        let context = CGContext(
            data: nil, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )!
        return ThumbnailImage(cgImage: context.makeImage()!)
    }

    private static func writeJPEG(width: Int, height: Int) throws -> URL {
        let context = CGContext(
            data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )!
        context.setFillColor(CGColor(red: 0.2, green: 0.4, blue: 0.6, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let cgImage = context.makeImage()!
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("scr177-\(UUID().uuidString).jpg")
        let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(dest, cgImage, nil)
        guard CGImageDestinationFinalize(dest) else {
            throw NSError(domain: "test", code: 1)
        }
        return url
    }

    func testCacheServesSecondCallWithoutReDecoding() async {
        let counter = Counter()
        let image = Self.dummyThumbnail()
        let loader = ThumbnailLoader(decode: { _, _ in counter.increment(); return image })
        let url = URL(fileURLWithPath: "/tmp/scr177-cache.jpg")

        _ = await loader.thumbnail(for: url)
        _ = await loader.thumbnail(for: url)

        XCTAssertEqual(counter.count, 1, "second call should hit the cache, not re-decode")
    }

    func testConcurrentRequestsForSameURLCoalesceToOneDecode() async {
        let counter = Counter()
        let image = Self.dummyThumbnail()
        let loader = ThumbnailLoader(maxConcurrentDecodes: 1, decode: { _, _ in
            counter.increment()
            Thread.sleep(forTimeInterval: 0.05) // hold so the other requests overlap the in-flight decode
            return image
        })
        let url = URL(fileURLWithPath: "/tmp/scr177-coalesce.jpg")

        async let a = loader.thumbnail(for: url)
        async let b = loader.thumbnail(for: url)
        async let c = loader.thumbnail(for: url)
        _ = await (a, b, c)

        XCTAssertEqual(counter.count, 1, "concurrent requests for one URL must share a single decode")
    }

    func testMissingFileReturnsNil() async {
        let loader = ThumbnailLoader() // real decode
        let url = URL(fileURLWithPath: "/tmp/scr177-missing-\(UUID().uuidString).jpg")
        let image = await loader.thumbnail(for: url)
        XCTAssertNil(image)
    }

    func testNilResultIsNotCachedSoLaterCallRetries() async {
        // A frame absent at first resolve may appear later (still-processing
        // recording); a nil must not be cached or the row sticks on the placeholder.
        let counter = Counter()
        let image = Self.dummyThumbnail()
        let loader = ThumbnailLoader(decode: { _, _ in
            defer { counter.increment() }
            return counter.count == 0 ? nil : image
        })
        let url = URL(fileURLWithPath: "/tmp/scr177-retry.jpg")

        let first = await loader.thumbnail(for: url)
        let second = await loader.thumbnail(for: url)

        XCTAssertNil(first)
        XCTAssertNotNil(second)
        XCTAssertEqual(counter.count, 2, "nil result must not be cached; the second call re-decodes")
    }

    func testDecodeDownsamplesToMaxPixelSize() throws {
        let url = try Self.writeJPEG(width: 400, height: 300)
        defer { try? FileManager.default.removeItem(at: url) }

        let image = ThumbnailLoader.decodeDownsampled(url: url, maxPixelSize: 100)
        let cgImage = try XCTUnwrap(image?.cgImage)
        XCTAssertLessThanOrEqual(max(cgImage.width, cgImage.height), 100)
        XCTAssertGreaterThan(min(cgImage.width, cgImage.height), 0)
    }
}
