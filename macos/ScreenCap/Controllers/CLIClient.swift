import Foundation

enum CLIError: LocalizedError {
    case binaryNotFound(searchedPaths: [String])
    case nonZeroExit(code: Int32, stderr: String)
    case decode(underlying: Error, raw: String)
    case launchFailed(underlying: Error)
    case timedOut(seconds: TimeInterval)

    var errorDescription: String? {
        switch self {
        case .binaryNotFound(let paths):
            return "screencap CLI not found. Searched: \(paths.joined(separator: ", "))"
        case .nonZeroExit(let code, let stderr):
            return "screencap exited with code \(code): \(stderr)"
        case .decode(let underlying, _):
            return "Failed to decode JSON from screencap: \(underlying.localizedDescription)"
        case .launchFailed(let underlying):
            return "Failed to launch screencap: \(underlying.localizedDescription)"
        case .timedOut(let seconds):
            return "screencap timed out after \(Int(seconds))s and was terminated."
        }
    }
}

/// Owns Process spawning, stderr line streaming, and JSON parsing for the bundled
/// screencap CLI. Resolves the binary from `Contents/Resources/screencap/screencap`
/// in the bundled app, with `SCREENCAP_CLI_PATH` env-var override for development.
final class CLIClient {
    /// Long-lived subprocess handle. Caller retains it for the duration of the
    /// recording lifecycle and calls `terminate()` to send SIGTERM.
    final class SpawnedProcess {
        let process: Process
        let stderrPipe: Pipe
        let stdoutPipe: Pipe

        init(process: Process, stderrPipe: Pipe, stdoutPipe: Pipe) {
            self.process = process
            self.stderrPipe = stderrPipe
            self.stdoutPipe = stdoutPipe
        }

        var isRunning: Bool { process.isRunning }
        var processIdentifier: Int32 { process.processIdentifier }

        func terminate() {
            if process.isRunning {
                process.terminate()
            }
        }

        func waitUntilExit() async {
            await withCheckedContinuation { continuation in
                if !process.isRunning {
                    continuation.resume()
                    return
                }
                let oldHandler = process.terminationHandler
                process.terminationHandler = { proc in
                    oldHandler?(proc)
                    continuation.resume()
                }
            }
        }
    }

    /// Resolves the screencap binary path. Order:
    /// 1. `SCREENCAP_CLI_PATH` environment variable (dev override).
    /// 2. `Contents/Resources/screencap/screencap` inside the app bundle.
    /// 3. `python -m screencap.cli` via `PYTHONPATH=src` if the repo is detectable
    ///    (dev fallback when running from Xcode without a pyinstaller build).
    static func resolveBinary() throws -> (executable: URL, leadingArgs: [String]) {
        var searched: [String] = []

        if let override = ProcessInfo.processInfo.environment["SCREENCAP_CLI_PATH"], !override.isEmpty {
            let url = URL(fileURLWithPath: override)
            searched.append(url.path)
            if FileManager.default.isExecutableFile(atPath: url.path) {
                return (url, [])
            }
        }

        if let resourceURL = Bundle.main.resourceURL {
            let bundled = resourceURL.appendingPathComponent("screencap/screencap")
            searched.append(bundled.path)
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return (bundled, [])
            }
        }

        // Dev fallback: shell out to `python -m screencap.cli` with PYTHONPATH set.
        // The repo root is assumed two levels above the bundle's MacOS directory
        // when running from Xcode; the env var SCREENCAP_DEV_REPO_ROOT can override.
        if let repoRoot = ProcessInfo.processInfo.environment["SCREENCAP_DEV_REPO_ROOT"], !repoRoot.isEmpty {
            let python = URL(fileURLWithPath: "/usr/bin/env")
            return (python, ["python3", "-m", "screencap.cli"])
        }

        throw CLIError.binaryNotFound(searchedPaths: searched)
    }

    /// Runs a one-shot CLI command and decodes its stdout as JSON.
    ///
    /// Pipes are drained concurrently — `screencap list --json` for power
    /// users can exceed the default 64KB pipe buffer, which would deadlock
    /// the child if we waited for exit before reading. A separate timeout
    /// task SIGTERMs the process if it overruns the deadline.
    static func runJSON<T: Decodable>(_ args: [String], timeout: TimeInterval = 10) async throws -> T {
        let (executable, leading) = try resolveBinary()
        let process = Process()
        process.executableURL = executable
        process.arguments = leading + args
        process.environment = mergedEnv()

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        do {
            try process.run()
        } catch {
            throw CLIError.launchFailed(underlying: error)
        }

        // Drain pipes concurrently — without this, >64KB output deadlocks
        // the child. Each task reads to EOF (which only happens once the
        // child closes its end, i.e., on exit).
        async let stdoutData: Data = readAllInBackground(stdout.fileHandleForReading)
        async let stderrData: Data = readAllInBackground(stderr.fileHandleForReading)

        // Race subprocess exit against the timeout. If the timeout wins,
        // SIGTERM the process so its FDs close and the drain tasks unblock.
        let didTimeOut = await raceExitAgainstTimeout(process: process, timeout: timeout)

        let stdoutBytes = await stdoutData
        let stderrBytes = await stderrData

        if didTimeOut {
            throw CLIError.timedOut(seconds: timeout)
        }

        let exitCode = process.terminationStatus
        if exitCode != 0 {
            let errText = String(data: stderrBytes, encoding: .utf8) ?? "<binary>"
            throw CLIError.nonZeroExit(code: exitCode, stderr: errText)
        }

        do {
            return try JSONDecoder().decode(T.self, from: stdoutBytes)
        } catch {
            let raw = String(data: stdoutBytes, encoding: .utf8) ?? "<binary>"
            throw CLIError.decode(underlying: error, raw: raw)
        }
    }

    /// Reads `handle` to EOF on a background queue. The continuation resumes
    /// once the child closes its end of the pipe (typically on exit).
    private static func readAllInBackground(_ handle: FileHandle) async -> Data {
        await withCheckedContinuation { (continuation: CheckedContinuation<Data, Never>) in
            DispatchQueue.global(qos: .userInitiated).async {
                let data = (try? handle.readToEnd()) ?? Data()
                continuation.resume(returning: data)
            }
        }
    }

    /// Returns `true` if the timeout fired before the process exited. On
    /// timeout, SIGTERM is sent so pipe drain tasks can complete.
    private static func raceExitAgainstTimeout(process: Process, timeout: TimeInterval) async -> Bool {
        await withTaskGroup(of: Bool.self) { group in
            group.addTask {
                await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                    DispatchQueue.global(qos: .userInitiated).async {
                        process.waitUntilExit()
                        cont.resume()
                    }
                }
                return false
            }
            group.addTask {
                let nanos = UInt64(max(0, timeout) * 1_000_000_000)
                try? await Task.sleep(nanoseconds: nanos)
                return true
            }
            // First task to finish wins; cancel the other.
            let result = await group.next() ?? false
            group.cancelAll()
            if result, process.isRunning {
                process.terminate()
                // Wait briefly for the SIGTERM to take effect so the drain
                // tasks see EOF and don't hang on `readToEnd()`.
                await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                    DispatchQueue.global(qos: .userInitiated).async {
                        process.waitUntilExit()
                        cont.resume()
                    }
                }
            }
            return result
        }
    }

    /// Spawns a long-lived subprocess and streams stderr lines to `onStderrLine`.
    /// Used by RecorderController for `screencap start` (event contract per Unit 8a).
    /// The caller retains the returned SpawnedProcess and calls `terminate()` to stop.
    static func spawn(
        args: [String],
        extraEnv: [String: String] = [:],
        onStderrLine: @escaping @Sendable (String) -> Void,
        onStdoutLine: (@Sendable (String) -> Void)? = nil
    ) throws -> SpawnedProcess {
        let (executable, leading) = try resolveBinary()
        let process = Process()
        process.executableURL = executable
        process.arguments = leading + args
        process.environment = mergedEnv(extra: extraEnv)

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let stderrBuffer = LineBuffer(handler: onStderrLine)
        stderrPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { return }
            stderrBuffer.feed(data)
        }

        if let onStdoutLine {
            let stdoutBuffer = LineBuffer(handler: onStdoutLine)
            stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty { return }
                stdoutBuffer.feed(data)
            }
        }

        process.terminationHandler = { _ in
            stderrPipe.fileHandleForReading.readabilityHandler = nil
            stdoutPipe.fileHandleForReading.readabilityHandler = nil
            stderrBuffer.flush()
        }

        do {
            try process.run()
        } catch {
            throw CLIError.launchFailed(underlying: error)
        }

        return SpawnedProcess(process: process, stderrPipe: stderrPipe, stdoutPipe: stdoutPipe)
    }

    /// Fire-and-forget invocation. Used for `screencap view <name>` (Unit 14a) where
    /// we don't care about the result — macOS opens the user's default browser.
    @discardableResult
    static func runDetached(_ args: [String]) throws -> Process {
        let (executable, leading) = try resolveBinary()
        let process = Process()
        process.executableURL = executable
        process.arguments = leading + args
        process.environment = mergedEnv()
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            throw CLIError.launchFailed(underlying: error)
        }
        return process
    }

    /// Builds the subprocess environment by merging extras into the current
    /// environment, then forcing SCREENCAP_PARENT and PYTHONUNBUFFERED. We never
    /// replace the inherited environment — that would break PATH / TMPDIR / etc.
    /// When SCREENCAP_DEV_REPO_ROOT is set, prepends `<repo>/src` to PYTHONPATH
    /// so `python3 -m screencap.cli` resolves the package without an editable
    /// install. Without this, the dev fallback fails with ModuleNotFoundError.
    private static func mergedEnv(extra: [String: String] = [:]) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        for (k, v) in extra { env[k] = v }
        env["SCREENCAP_PARENT"] = "swiftui"
        env["PYTHONUNBUFFERED"] = "1"
        if let repoRoot = env["SCREENCAP_DEV_REPO_ROOT"], !repoRoot.isEmpty {
            let srcPath = repoRoot + "/src"
            if let existing = env["PYTHONPATH"], !existing.isEmpty {
                env["PYTHONPATH"] = "\(srcPath):\(existing)"
            } else {
                env["PYTHONPATH"] = srcPath
            }
        }
        return env
    }
}

/// Concatenates streamed bytes into UTF-8 lines and dispatches them to a handler.
/// Pipe `readabilityHandler` callbacks deliver arbitrary chunks, not lines.
private final class LineBuffer: @unchecked Sendable {
    private let handler: @Sendable (String) -> Void
    private let lock = NSLock()
    private var pending = Data()

    init(handler: @escaping @Sendable (String) -> Void) {
        self.handler = handler
    }

    func feed(_ data: Data) {
        lock.lock()
        pending.append(data)
        var lines: [String] = []
        while let nl = pending.firstIndex(of: 0x0A) {
            let line = pending.subdata(in: pending.startIndex..<nl)
            pending.removeSubrange(pending.startIndex...nl)
            if let s = String(data: line, encoding: .utf8) {
                lines.append(s)
            }
        }
        lock.unlock()
        for line in lines { handler(line) }
    }

    func flush() {
        lock.lock()
        let trailing = pending
        pending.removeAll()
        lock.unlock()
        if !trailing.isEmpty, let s = String(data: trailing, encoding: .utf8) {
            handler(s)
        }
    }
}
