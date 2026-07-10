import Darwin
import Foundation

/// Abstract surface over `CLIClient.SpawnedProcess` so the orchestrator's
/// Cmd+Q-timeout SIGKILL path can be exercised with a fake that doesn't
/// actually signal a real PID. The two getter properties expose the existing
/// `Process` accessors; `forceKill()` returns true when the SIGKILL was
/// dispatched so callers can gate user-facing "force-killed" messaging on it.
protocol SpawnedProcessHandle: AnyObject {
    var isRunning: Bool { get }
    var processIdentifier: Int32 { get }
    /// SIGTERM equivalent — graceful shutdown request. Plan U7's UploadController
    /// invokes this on window-close-as-cancel; the Python side's SIGTERM
    /// handler (U1) converts it to a KeyboardInterrupt-equivalent path that
    /// emits `upload_failed(error: "interrupted")` before exiting.
    func terminate()
    @discardableResult
    func forceKill() -> Bool
}

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
/// screencap CLI. Resolves the binary from the helper bundle at
/// `Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap`
/// (SCR-196), with `SCREENCAP_CLI_PATH` env-var override for development.
/// Daemon-backed app flows should use `DaemonClient`; this client remains as the
/// Phase 1 reversibility and unavailable-daemon fallback path.
///
/// Caseless enum — every member is `static`, there is no instance state, and
/// `enum` prevents accidental instantiation that a `class` would allow.
enum CLIClient {
    /// Long-lived subprocess handle. Caller retains it for the duration of the
    /// recording lifecycle and calls `terminate()` to send SIGTERM.
    final class SpawnedProcess: SpawnedProcessHandle {
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

        /// Send SIGKILL to the subprocess if it's still alive. Returns true if
        /// the signal was actually dispatched (process was running and had a
        /// valid PID). Guarded so a recycled PID from an unrelated process
        /// cannot be signalled — macOS reuses PIDs quickly after exit, and
        /// `processIdentifier` keeps returning the original PID even after
        /// the child is gone.
        @discardableResult
        func forceKill() -> Bool {
            guard process.isRunning, process.processIdentifier > 0 else { return false }
            kill(process.processIdentifier, SIGKILL)
            return true
        }

        func waitUntilExit() async {
            // Use a blocking waitUntilExit on a background queue rather than
            // chaining terminationHandler. The handler-chaining variant has a
            // TOCTOU window: if `isRunning` is true at the check but the child
            // exits before the handler assignment lands, the new handler is
            // never invoked and the continuation never resumes.
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global(qos: .userInitiated).async { [process] in
                    process.waitUntilExit()
                    continuation.resume()
                }
            }
        }
    }

    /// Resolves the screencap binary path. Order:
    /// 1. `SCREENCAP_CLI_PATH` environment variable (dev override).
    /// 2. `Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap`
    ///    inside the app bundle (the SCR-196 helper bundle).
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

        // SCR-196: the daemon/CLI now ships as a helper .app (bundle id
        // com.screencap.daemon) in Contents/Library/LoginItems/, so the binary
        // lives at the bundle's Contents/MacOS/, not the old bare
        // Contents/Resources/screencap/.
        let bundled = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap")
        searched.append(bundled.path)
        if FileManager.default.isExecutableFile(atPath: bundled.path) {
            return (bundled, [])
        }

        // Dev fallback: shell out to `python -m screencap.cli` with PYTHONPATH
        // set so the repo's `src/` is importable. Gated to Debug so a Release
        // build can't be redirected at an arbitrary Python module via env var.
        #if DEBUG
        if let repoRoot = ProcessInfo.processInfo.environment["SCREENCAP_DEV_REPO_ROOT"], !repoRoot.isEmpty {
            searched.append("dev fallback via SCREENCAP_DEV_REPO_ROOT=\(repoRoot)")
            let python = URL(fileURLWithPath: "/usr/bin/env")
            return (python, ["python3", "-m", "screencap.cli"])
        }
        #endif

        throw CLIError.binaryNotFound(searchedPaths: searched)
    }

    /// Runs a one-shot CLI command and decodes its stdout as JSON.
    ///
    /// Pipes are drained concurrently — `screencap list --json` for power
    /// users can exceed the default 64KB pipe buffer, which would deadlock
    /// the child if we waited for exit before reading. A separate timeout
    /// task SIGTERMs the process if it overruns the deadline.
    ///
    /// Callers must pass `--json` explicitly. The CLI auto-detects non-TTY
    /// stdout and flips into JSON mode today, but that's a coincidence of
    /// the implementation we should not rely on — a future CLI refactor
    /// (or a Rich-styled debug pane upstream) would silently start sending
    /// prose and `JSONDecoder` would reject it.
    static func runJSON<T: Decodable>(_ args: [String], timeout: TimeInterval = 10) async throws -> T {
        assert(args.contains("--json"), "runJSON requires the caller to pass --json explicitly; relying on TTY auto-detect is fragile. args=\(args)")
        let (stdoutBytes, stderrBytes) = try await runOneShot(args, timeout: timeout)
        do {
            return try JSONDecoder().decode(T.self, from: stdoutBytes)
        } catch {
            let raw = String(data: stdoutBytes, encoding: .utf8) ?? "<binary>"
            // stderr may carry a hint even on JSON-decode failure paths.
            _ = stderrBytes
            throw CLIError.decode(underlying: error, raw: raw)
        }
    }

    /// Runs a one-shot CLI command, awaits exit, and throws on non-zero.
    /// Used for shell-outs the user expects to "just work" (e.g.
    /// `screencap view <name>` from a row click) where a silent failure
    /// would look like a broken click.
    static func runAwaitingExit(_ args: [String], timeout: TimeInterval = 10) async throws {
        _ = try await runOneShot(args, timeout: timeout)
    }

    /// Like `runJSON` but returns the raw stdout bytes instead of a decoded
    /// model. Callers decode themselves — useful when the same controller
    /// invokes several distinct JSON endpoints and wants one chokepoint for
    /// process spawning + test injection, or when the caller needs to inspect
    /// the `ok` / `error` envelope fields before deciding what to decode into
    /// a typed model.
    static func runJSONRaw(_ args: [String], timeout: TimeInterval = 10) async throws -> Data {
        assert(args.contains("--json"), "runJSONRaw requires the caller to pass --json explicitly. args=\(args)")
        let (stdoutBytes, _) = try await runOneShot(args, timeout: timeout)
        return stdoutBytes
    }

    /// Like `runJSONRaw` but pipes `stdin` to the child's standard input, for
    /// commands whose secret payload must NOT transit argv (KTD3 — argv is
    /// world-readable via `ps`). The BYO `settings intelligence --set-key`
    /// path reads the API key from stdin; the key is written to the pipe here
    /// and never appears in `arguments`. Returns raw stdout bytes.
    ///
    /// The stdin pipe replaces the usual `/dev/null` stdin; the CLI's
    /// `--set-key` handler explicitly reads stdin (it does not gate on a TTY
    /// prompt), so the LLDB-pty concern that motivates `/dev/null` elsewhere
    /// does not apply on this path.
    ///
    /// Non-zero exit is tolerated and the stdout envelope returned, because the
    /// BYO `--set-key --validate` path exits non-zero on an *invalid* key while
    /// still emitting a JSON `{"ok":false,"validation":"invalid",...}` envelope
    /// the caller must decode to distinguish "rejected key" from "launch/crash".
    static func runJSONRawStdin(
        _ args: [String], stdin: Data, timeout: TimeInterval = 10
    ) async throws -> Data {
        assert(args.contains("--json"), "runJSONRawStdin requires the caller to pass --json explicitly. args=\(args)")
        let (stdoutBytes, _) = try await runOneShot(
            args, timeout: timeout, stdin: stdin, allowNonZeroExit: true
        )
        return stdoutBytes
    }

    /// Like `runJSONRaw` but tolerates a non-zero exit and still returns the
    /// stdout envelope. For commands that exit non-zero on a *handled* refusal
    /// while still emitting a JSON `{"ok":false,"reason":…,"message":…}` body
    /// the caller must decode to distinguish a refusal (e.g. SCR-228
    /// `storage migrate` rejecting a cross-volume/cloud-synced target) from a
    /// launch/crash. Mirrors `runJSONRawStdin`'s `allowNonZeroExit` rationale.
    static func runJSONRawTolerant(_ args: [String], timeout: TimeInterval = 15) async throws -> Data {
        assert(args.contains("--json"), "runJSONRawTolerant requires the caller to pass --json explicitly. args=\(args)")
        let (stdoutBytes, _) = try await runOneShot(
            args, timeout: timeout, allowNonZeroExit: true
        )
        return stdoutBytes
    }

    /// Spawn + drain + race timeout. Returns (stdout, stderr); throws on
    /// launch failure, timeout, or non-zero exit. Shared backbone for
    /// `runJSON` and `runAwaitingExit` so the timeout / pipe-drain logic
    /// only lives in one place.
    private static func runOneShot(
        _ args: [String], timeout: TimeInterval, stdin: Data? = nil,
        allowNonZeroExit: Bool = false
    ) async throws -> (Data, Data) {
        let (executable, leading) = try resolveBinary()
        let process = Process()
        process.executableURL = executable
        process.arguments = leading + args
        process.environment = mergedEnv()

        // Default: /dev/null on stdin so the child's `sys.stdin.isatty()`
        // returns false. SwiftUI is non-interactive, but Xcode's Run launches
        // the app under LLDB which exposes a pty as stdin — without this, the
        // inherited fd looks like a real terminal and the recorder's
        // `if isatty(): prompt` guards bypass, blocking forever on
        // `click.confirm` / `click.prompt`.
        //
        // When `stdin` is supplied (the BYO --set-key secret path, KTD3), pipe
        // it instead: the CLI reads the key off stdin so it never transits argv.
        let stdinPipe: Pipe?
        if stdin != nil {
            let pipe = Pipe()
            process.standardInput = pipe
            stdinPipe = pipe
        } else {
            process.standardInput = FileHandle.nullDevice
            stdinPipe = nil
        }

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        do {
            try process.run()
        } catch {
            throw CLIError.launchFailed(underlying: error)
        }

        // Write the secret and close the write end so the child sees EOF and
        // its `sys.stdin.read()` returns. Done off the main path to avoid a
        // deadlock if the payload ever exceeds the pipe buffer. A close-error
        // is harmless — if the child already exited we still drain stdout below.
        if let stdinPipe, let payload = stdin {
            let handle = stdinPipe.fileHandleForWriting
            DispatchQueue.global(qos: .userInitiated).async {
                try? handle.write(contentsOf: payload)
                try? handle.close()
            }
        }

        // SIGTERM the child if the awaiting Task is cancelled — e.g. the review
        // window is dismissed while `review-data` is mid-scrub. Without this the
        // scrub (a full NER pass, up to the timeout) keeps running orphaned.
        // Capture the pid (Sendable) rather than the Process so the @Sendable
        // onCancel closure stays concurrency-clean.
        let pid = process.processIdentifier
        return try await withTaskCancellationHandler {
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
            if exitCode != 0 && !allowNonZeroExit {
                let errText = String(data: stderrBytes, encoding: .utf8) ?? "<binary>"
                throw CLIError.nonZeroExit(code: exitCode, stderr: errText)
            }

            return (stdoutBytes, stderrBytes)
        } onCancel: {
            if pid > 0 {
                kill(pid, SIGTERM)
            }
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

    /// Returns `true` if the timeout fired before the process exited.
    ///
    /// On timeout: SIGTERM the child, wait at most `terminateGrace` for it to
    /// react, then SIGKILL if it's still alive. Without this bounded grace, a
    /// CLI that ignores SIGTERM (stuck syscall, blocked signal) leaves
    /// `runJSON` waiting forever — defeating the timeout entirely.
    private static func raceExitAgainstTimeout(
        process: Process,
        timeout: TimeInterval,
        terminateGrace: TimeInterval = 2.0
    ) async -> Bool {
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
                let exited = await waitForExit(process: process, timeout: terminateGrace)
                if !exited, process.isRunning, process.processIdentifier > 0 {
                    kill(process.processIdentifier, SIGKILL)
                    _ = await waitForExit(process: process, timeout: terminateGrace)
                }
            }
            return result
        }
    }

    /// Polls until `process` exits or `timeout` elapses. Returns true if exited.
    private static func waitForExit(process: Process, timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(max(0, timeout))
        while process.isRunning && Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000) // 50ms
        }
        return !process.isRunning
    }

    /// Spawns a long-lived subprocess and streams stderr lines to `onStderrLine`.
    /// Used by RecorderController for `screencap start` (event contract per Unit 8a).
    /// The caller retains the returned SpawnedProcess and calls `terminate()` to stop.
    /// `onTerminated` runs after the subprocess exits, after pipe cleanup.
    static func spawn(
        args: [String],
        extraEnv: [String: String] = [:],
        onStderrLine: @escaping @Sendable (String) -> Void,
        onStdoutLine: (@Sendable (String) -> Void)? = nil,
        onTerminated: (@Sendable (Int32) -> Void)? = nil
    ) throws -> SpawnedProcess {
        let (executable, leading) = try resolveBinary()
        let process = Process()
        process.executableURL = executable
        process.arguments = leading + args
        process.environment = mergedEnv(extra: extraEnv)
        // /dev/null on stdin — see runOneShot for the pty-via-LLDB rationale.
        // Critical for `screencap start` specifically: without this, prompts
        // in the recorder init path (e.g. NLP-models confirm, scrub confirm)
        // see isatty()=true under Xcode and block forever on stdin read.
        process.standardInput = FileHandle.nullDevice

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

        // Always drain stdout, even when the caller doesn't ask for lines.
        // Python `screencap start` writes Rich-console banners and progress
        // to stdout; if no reader is attached the 64KB pipe buffer fills,
        // the child blocks on write, and stderr events stop flowing.
        let stdoutBufferOpt: LineBuffer? = onStdoutLine.map { LineBuffer(handler: $0) }
        stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { return }
            // The read itself drains the pipe; the buffer is only used when a
            // caller wants the lines. Without a caller we discard.
            stdoutBufferOpt?.feed(data)
        }

        process.terminationHandler = { proc in
            // Order matters and so does completeness:
            // 1. Nil the readabilityHandler so no further chunks dispatch.
            // 2. Drain anything still in the pipe to EOF — Apple does NOT
            //    guarantee that the last readabilityHandler callback fires
            //    before terminationHandler. If the child's final write
            //    (e.g. `stopped` or `recording_finalized` event) lands in
            //    the pipe right before exit, the handler may be skipped
            //    and those bytes would die with the FD. The child already
            //    closed its end, so readToEnd returns immediately with
            //    whatever's buffered.
            // 3. Flush the LineBuffer (any final partial line goes out).
            // 4. Notify the optional `onTerminated` callback with the exit
            //    code so RecorderController can react to process exit.
            stderrPipe.fileHandleForReading.readabilityHandler = nil
            stdoutPipe.fileHandleForReading.readabilityHandler = nil
            if let remaining = try? stderrPipe.fileHandleForReading.readToEnd(),
               !remaining.isEmpty {
                stderrBuffer.feed(remaining)
            }
            if let remaining = try? stdoutPipe.fileHandleForReading.readToEnd(),
               !remaining.isEmpty {
                stdoutBufferOpt?.feed(remaining)
            }
            stderrBuffer.flush()
            stdoutBufferOpt?.flush()
            onTerminated?(proc.terminationStatus)
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
        // /dev/null on stdin — see runOneShot for rationale.
        process.standardInput = FileHandle.nullDevice
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        // Discard whatever the child writes. Without a reader, >64KB of
        // output fills the pipe buffer and the child blocks on write.
        let drain: @Sendable (FileHandle) -> Void = { _ = $0.availableData }
        stdout.fileHandleForReading.readabilityHandler = drain
        stderr.fileHandleForReading.readabilityHandler = drain
        process.terminationHandler = { _ in
            stdout.fileHandleForReading.readabilityHandler = nil
            stderr.fileHandleForReading.readabilityHandler = nil
        }
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
    /// In Debug, when SCREENCAP_DEV_REPO_ROOT is set, prepends `<repo>/src` to
    /// PYTHONPATH so `python3 -m screencap.cli` resolves the package without an
    /// editable install. Release builds skip the PYTHONPATH override so an env
    /// var can't redirect the bundled CLI to an arbitrary module path.
    private static func mergedEnv(extra: [String: String] = [:]) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        for (k, v) in extra { env[k] = v }
        env["SCREENCAP_PARENT"] = "swiftui"
        env["PYTHONUNBUFFERED"] = "1"
        #if DEBUG
        if let repoRoot = env["SCREENCAP_DEV_REPO_ROOT"], !repoRoot.isEmpty {
            let srcPath = repoRoot + "/src"
            if let existing = env["PYTHONPATH"], !existing.isEmpty {
                env["PYTHONPATH"] = "\(srcPath):\(existing)"
            } else {
                env["PYTHONPATH"] = srcPath
            }
        }
        #endif
        return env
    }
}

/// Concatenates streamed bytes into UTF-8 lines and dispatches them to a handler.
/// Pipe `readabilityHandler` callbacks deliver arbitrary chunks, not lines.
///
/// Thread safety: `feed()` and `flush()` share an internal `NSLock`, so a
/// `feed()` call that races a `flush()` (e.g. a final readabilityHandler
/// callback that fires after we've nil'd the handler) is well-defined — both
/// see a consistent `pending` buffer.
///
/// Callers are responsible for ensuring no further `feed()` will arrive after
/// `flush()` returns. The spawn termination handler upholds this by nilling
/// the readabilityHandler before calling `flush()`.
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
