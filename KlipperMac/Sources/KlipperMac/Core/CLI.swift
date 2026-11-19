import Foundation

struct CLIResult {
    let exit: Int32
    let output: String
}

enum CLIError: LocalizedError {
    case notFound
    case timedOut(command: String, seconds: Int)
    var errorDescription: String? {
        switch self {
        case .notFound:
            return "Klipper CLI not found. Install it with the one-liner from the project README."
        case let .timedOut(command, seconds):
            return "\(command) timed out after \(seconds)s."
        }
    }
}

private final class ProcessFinishState: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false
    private var timeoutTimer: DispatchSourceTimer?

    func setTimer(_ timer: DispatchSourceTimer) {
        lock.lock()
        timeoutTimer = timer
        lock.unlock()
    }

    func finish<T>(_ result: Result<T, Error>,
                   cleanup: () -> Void = {},
                   resume: (Result<T, Error>) -> Void) {
        lock.lock()
        guard !finished else {
            lock.unlock()
            return
        }
        finished = true
        let timer = timeoutTimer
        timeoutTimer = nil
        lock.unlock()
        cleanup()
        timer?.cancel()
        resume(result)
    }
}

/// Runs `klipperformac <args>` — arguments are passed as an array, never through a shell.
final class CLIRunner: @unchecked Sendable {
    static let shared = CLIRunner()

    private static func childEnvironment() -> [String: String] {
        var e = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let extra = [home + "/.local/bin", "/usr/local/bin", "/opt/homebrew/bin",
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let existing = (e["PATH"] ?? "").split(separator: ":").map(String.init)
        var seen = Set<String>()
        var path: [String] = []
        for p in extra + existing where !seen.contains(p) {
            seen.insert(p)
            path.append(p)
        }
        e["PATH"] = path.joined(separator: ":")
        e.removeValue(forKey: "PYTHONPATH") // the launcher owns this
        return e
    }

    private static func commandLabel(_ args: [String]) -> String {
        "klipperformac " + args.joined(separator: " ")
    }

    func run(_ args: [String], timeout: TimeInterval? = nil) async throws -> CLIResult {
        guard let exe = KEnvironment.shared.cliURL else { throw CLIError.notFound }
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = exe
                p.arguments = args
                p.environment = Self.childEnvironment()
                let pipe = Pipe()
                p.standardOutput = pipe
                p.standardError = pipe
                p.standardInput = FileHandle.nullDevice
                let state = ProcessFinishState()
                let finish: (Result<CLIResult, Error>) -> Void = { result in
                    state.finish(result) { outcome in
                        cont.resume(with: outcome)
                    }
                }
                do {
                    try p.run()
                } catch {
                    finish(.failure(error))
                    return
                }
                if let timeout {
                    let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .userInitiated))
                    timer.schedule(deadline: .now() + timeout)
                    timer.setEventHandler {
                        if p.isRunning { p.terminate() }
                        finish(.failure(CLIError.timedOut(command: Self.commandLabel(args),
                                                          seconds: max(1, Int(timeout.rounded())))))
                    }
                    state.setTimer(timer)
                    timer.resume()
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                let text = String(data: data, encoding: .utf8)
                    ?? String(decoding: data, as: UTF8.self)
                finish(.success(CLIResult(exit: p.terminationStatus, output: text)))
            }
        }
    }

    /// Long-running commands (setup, update --apply): stream lines as they arrive.
    func stream(_ args: [String],
                timeout: TimeInterval? = nil,
                onLines: @escaping @Sendable ([String]) -> Void) async throws -> Int32 {
        guard let exe = KEnvironment.shared.cliURL else { throw CLIError.notFound }
        return try await withCheckedThrowingContinuation { cont in
            let p = Process()
            p.executableURL = exe
            p.arguments = args
            p.environment = Self.childEnvironment()
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            p.standardInput = FileHandle.nullDevice
            let buffer = LineBuffer()
            let state = ProcessFinishState()
            let finish: (Result<Int32, Error>) -> Void = { result in
                state.finish(result,
                             cleanup: { pipe.fileHandleForReading.readabilityHandler = nil }) { outcome in
                    cont.resume(with: outcome)
                }
            }
            pipe.fileHandleForReading.readabilityHandler = { h in
                let d = h.availableData
                if d.isEmpty { return }
                let lines = buffer.append(d)
                if !lines.isEmpty { onLines(lines) }
            }
            p.terminationHandler = { proc in
                let lines = buffer.flush()
                if !lines.isEmpty { onLines(lines) }
                finish(.success(proc.terminationStatus))
            }
            do {
                try p.run()
            } catch {
                finish(.failure(error))
                return
            }
            if let timeout {
                let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .userInitiated))
                timer.schedule(deadline: .now() + timeout)
                timer.setEventHandler {
                    if p.isRunning { p.terminate() }
                    finish(.failure(CLIError.timedOut(command: Self.commandLabel(args),
                                                      seconds: max(1, Int(timeout.rounded())))))
                }
                state.setTimer(timer)
                timer.resume()
            }
        }
    }
}

final class LineBuffer: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()

    func append(_ chunk: Data) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        data.append(chunk)
        var lines: [String] = []
        var start = data.startIndex
        var consumedUpTo = data.startIndex
        while let nl = data[start...].firstIndex(of: 0x0A) {
            let lineData = data.subdata(in: start..<nl)
            if let s = String(data: lineData, encoding: .utf8) { lines.append(s) }
            let next = data.index(after: nl)
            start = next
            consumedUpTo = next
        }
        if consumedUpTo > data.startIndex {
            data.removeSubrange(data.startIndex..<consumedUpTo)
        }
        return lines
    }

    func flush() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        defer { data = Data() }
        guard let s = String(data: data, encoding: .utf8), !s.isEmpty else { return [] }
        return [s]
    }
}
