import Foundation

struct CLIResult {
    let exit: Int32
    let output: String
}

enum CLIError: LocalizedError {
    case notFound
    var errorDescription: String? {
        switch self {
        case .notFound:
            return "Klipper CLI not found. Install it with the one-liner from the project README."
        }
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

    func run(_ args: [String]) async throws -> CLIResult {
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
                do {
                    try p.run()
                } catch {
                    cont.resume(throwing: error)
                    return
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                let text = String(data: data, encoding: .utf8)
                    ?? String(decoding: data, as: UTF8.self)
                cont.resume(returning: CLIResult(exit: p.terminationStatus, output: text))
            }
        }
    }

    /// Long-running commands (setup, update --apply): stream lines as they arrive.
    func stream(_ args: [String], onLine: @escaping @Sendable (String) -> Void) async throws -> Int32 {
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
            pipe.fileHandleForReading.readabilityHandler = { h in
                let d = h.availableData
                if d.isEmpty { return }
                for line in buffer.append(d) { onLine(line) }
            }
            p.terminationHandler = { proc in
                pipe.fileHandleForReading.readabilityHandler = nil
                for line in buffer.flush() { onLine(line) }
                cont.resume(returning: proc.terminationStatus)
            }
            do {
                try p.run()
            } catch {
                cont.resume(throwing: error)
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
        while let nl = data.firstIndex(of: 0x0A) {
            let lineData = data.subdata(in: data.startIndex..<nl)
            data.removeSubrange(data.startIndex...nl)
            if let s = String(data: lineData, encoding: .utf8) { lines.append(s) }
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
