import Foundation

struct ServiceState {
    let name: String
    let up: Bool
}

struct StackSnapshot {
    enum Overall { case stopped, running, notResponding }

    /// state.json exists and belongs to this boot (stack was started at some point)
    var tracked = false
    var services: [ServiceState] = []
    var startedAt: String?

    var anyUp: Bool { services.contains { $0.up } }
    var overall: Overall {
        if !tracked { return .stopped }
        return anyUp ? .running : .notResponding
    }
}

/// Re-implements process.running() from process.py: re-validate every pid on
/// read (pid + cmdline marker + boot time), so stale state self-heals.
enum StackReader {
    static let order = ["web_mainsail", "web_fluidd", "moonraker", "klipper", "caffeinate"]

    static func pretty(_ name: String) -> String {
        switch name {
        case "web_mainsail": return "Mainsail web"
        case "web_fluidd": return "Fluidd web"
        case "caffeinate": return "Sleep guard"
        case "klipper": return "Klipper"
        case "moonraker": return "Moonraker"
        default: return name
        }
    }

    private static var bootCache: (at: Date, value: Optional<String>) = (.distantPast, nil)
    private static let bootLock = NSLock()

    static func bootTime() -> String? {
        bootLock.lock()
        defer { bootLock.unlock() }
        if Date().timeIntervalSince(bootCache.at) < 5 { return bootCache.value }
        let out = runTool("/usr/sbin/sysctl", ["-n", "kern.boottime"]) ?? ""
        // sysctl prints "{ sec = 1759734000, usec = 0 } ..." — take the digits
        // right after "sec = " (matches process.py regex "sec = (\d+)").
        var value: String?
        if let range = out.range(of: "sec = ") {
            let digits = out[range.upperBound...].prefix(while: { $0.isNumber })
            if !digits.isEmpty { value = String(digits) }
        }
        bootCache = (Date(), value)
        return value
    }

    static func readSync(env: KEnvironment) -> StackSnapshot {
        var snap = StackSnapshot()
        guard let data = try? Data(contentsOf: env.state),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["boot_time"] as? String == bootTime()
        else { return snap }

        snap.tracked = true
        snap.startedAt = obj["started"] as? String

        let markers: [String: String] = [
            "klipper": "klippy.py",
            "moonraker": "moonraker",
            "web_mainsail": "klipperformac.proxy \(env.webPort) ",
            "web_fluidd": "klipperformac.proxy \(env.fluiddPort) ",
            "caffeinate": "caffeinate",
        ]

        var pids: [String: Int] = [:]
        for name in order {
            if let pid = obj[name] as? Int { pids[name] = pid }
        }
        let idList = pids.values.map(String.init)
        var cmds: [String: String] = [:]
        if !idList.isEmpty, let out = runTool("/bin/ps", ["-p", idList.joined(separator: ","),
                                                          "-o", "pid=,state=,command="]) {
            for raw in out.split(separator: "\n") {
                let parts = raw.split(separator: " ", maxSplits: 2,
                                      omittingEmptySubsequences: true)
                guard parts.count == 3 else { continue }
                if parts[1].contains("Z") { continue } // zombie
                cmds[String(parts[0])] = String(parts[2])
            }
        }
        snap.services = order.map { name in
            let pid = pids[name].map(String.init)
            let cmd = pid.flatMap { cmds[$0] } ?? ""
            return ServiceState(name: name, up: pid != nil && cmd.contains(markers[name] ?? ""))
        }
        return snap
    }

    private static func runTool(_ exe: String, _ args: [String]) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(data: data, encoding: .utf8)
    }
}
