import Foundation

/// Installs a throwaway fake `screencap` binary and points `SCREENCAP_CLI_PATH`
/// at it, so `CLIClient` spawns the fake instead of the real CLI. The fake
/// writes a fixed `stdout` payload and exits with a fixed code, writing nothing
/// to stderr — the exact shape of the CLI's `--json` error path
/// (`{ok:false, error}` on stdout followed by `sys.exit(1)`). Call `remove()`
/// from `tearDown` to unset the override and delete the fake.
final class FakeCLIBinary {
    private let dir: URL

    /// Creates and installs the fake. `stdout` is emitted verbatim; `exitCode`
    /// becomes the process exit status.
    init(stdout: String, exitCode: Int) throws {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("fake-cli-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("screencap")
        // Single-quote the payload so JSON metacharacters stay inert; `printf
        // '%s'` prints its argument literally (no format expansion).
        let quoted = stdout.replacingOccurrences(of: "'", with: "'\\''")
        let script = "#!/bin/sh\nprintf '%s' '\(quoted)'\nexit \(exitCode)\n"
        try script.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: url.path
        )
        setenv("SCREENCAP_CLI_PATH", url.path, 1)
    }

    /// Unsets the override and deletes the fake. Safe to call more than once.
    func remove() {
        unsetenv("SCREENCAP_CLI_PATH")
        try? FileManager.default.removeItem(at: dir)
    }
}
