import Foundation

struct ServiceState: Equatable {
    let name: String
    let up: Bool
}

struct StackSnapshot: Equatable {
    enum Overall { case stopped, running, notResponding }

    /// state.json exists and belongs to this boot (stack was started at some point)
    var tracked = false
    var services: [ServiceState] = []
    var startedAt: String?
    /// Ports still held by this install's own services even when state.json is
    /// gone or stale, mirroring process.our_listeners() on the CLI side.
    var listenerPorts: Set<Int> = []

    var anyUp: Bool { services.contains { $0.up } || !listenerPorts.isEmpty }
    var overall: Overall {
        if anyUp { return .running }
        return tracked ? .notResponding : .stopped
    }
}

/// Re-implements process.running() from process.py: re-validate every pid on
/// read (pid + cmdline marker + boot time), so stale state self-heals.
enum StackReader {
    static let order = ["web_mainsail", "web_fluidd", "moonraker", "klipper", "caffeinate"]
    static let bootUnknown = "unknown"
    private static var cmdCache: (at: Date, key: String, value: [String: String]) =
        (.distantPast, "", [:])
    private static let cmdLock = NSLock()
    private static var listenerCache: (at: Date, key: String, value: [Int: [String]]) =
        (.distantPast, "", [:])
    private static let listenerLock = NSLock()

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
        // kern.boottime directly — no /usr/sbin/sysctl subprocess per poll.
        var tv = timeval(tv_sec: 0, tv_usec: 0)
        var size = MemoryLayout<timeval>.stride
        let rc = withUnsafeMutablePointer(to: &tv) {
            sysctlbyname("kern.boottime", $0, &size, nil, 0)
        }
        let value = rc == 0 ? String(tv.tv_sec) : nil
        bootCache = (Date(), value)
        return value
    }

    static func markers(webPort: Int, fluiddPort: Int) -> [String: String] {
        [
            "klipper": "klippy.py",
            "moonraker": "moonraker",
            "web_mainsail": "klipperformac.proxy \(webPort) ",
            "web_fluidd": "klipperformac.proxy \(fluiddPort) ",
            "caffeinate": "caffeinate",
        ]
    }

    static func snapshot(from state: [String: Any],
                         bootTime: String?,
                         markers: [String: String],
                         commands: [String: String],
                         listenerPorts: Set<Int> = []) -> StackSnapshot {
        var snap = StackSnapshot()
        snap.listenerPorts = listenerPorts
        guard let recorded = state["boot_time"] as? String else { return snap }
        if recorded != bootUnknown {
            guard let bootTime, recorded == bootTime else { return snap }
        }

        snap.tracked = true
        snap.startedAt = state["started"] as? String

        var pids: [String: Int] = [:]
        for name in order {
            if let pid = state[name] as? Int { pids[name] = pid }
        }
        snap.services = order.map { name in
            let pid = pids[name].map(String.init)
            let cmd = pid.flatMap { commands[$0] } ?? ""
            return ServiceState(name: name,
                                up: pid != nil && cmd.contains(markers[name] ?? ""))
        }
        return snap
    }

    static func readSync(env: KEnvironment) -> StackSnapshot {
        let markers = markers(webPort: env.webPort, fluiddPort: env.fluiddPort)
        let listenerMap = listenerPIDs([env.moonrakerPort, env.webPort, env.fluiddPort])
        let listenerIDs = Set(listenerMap.values.flatMap { $0 })
        guard let data = try? Data(contentsOf: env.state),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else {
            let commands = commandLines(for: Array(listenerIDs))
            return StackSnapshot(listenerPorts: listenerPorts(from: listenerMap,
                                                              markers: markers,
                                                              commands: commands))
        }
        var pids: [String: Int] = [:]
        for name in order {
            if let pid = obj[name] as? Int { pids[name] = pid }
        }
        let idList = Array(Set(pids.values.map(String.init)).union(listenerIDs))
        let cmds = commandLines(for: idList)
        return snapshot(from: obj,
                        bootTime: bootTime(),
                        markers: markers,
                        commands: cmds,
                        listenerPorts: listenerPorts(from: listenerMap,
                                                     markers: markers,
                                                     commands: cmds))
    }

    static func serviceKind(command: String, markers: [String: String]) -> String? {
        for name in order where command.contains(markers[name] ?? "") {
            return name
        }
        return nil
    }

    static func listenerPorts(from listeners: [Int: [String]],
                              markers: [String: String],
                              commands: [String: String]) -> Set<Int> {
        var ports: Set<Int> = []
        for (port, pids) in listeners {
            for pid in pids where serviceKind(command: commands[pid] ?? "",
                                              markers: markers) != nil {
                ports.insert(port)
                break
            }
        }
        return ports
    }

    private static func commandLines(for idList: [String]) -> [String: String] {
        guard !idList.isEmpty else { return [:] }
        let key = idList.sorted().joined(separator: ",")
        cmdLock.lock()
        if cmdCache.key == key, Date().timeIntervalSince(cmdCache.at) < 3 {
            let value = cmdCache.value
            cmdLock.unlock()
            return value
        }
        cmdLock.unlock()

        var cmds: [String: String] = [:]
        if let out = runTool("/bin/ps", ["-p", idList.joined(separator: ","),
                                         "-o", "pid=,state=,command="]) {
            for raw in out.split(separator: "\n") {
                let parts = raw.split(separator: " ", maxSplits: 2,
                                      omittingEmptySubsequences: true)
                guard parts.count == 3 else { continue }
                if parts[1].contains("Z") { continue } // zombie
                cmds[String(parts[0])] = String(parts[2])
            }
        }
        cmdLock.lock()
        cmdCache = (Date(), key, cmds)
        cmdLock.unlock()
        return cmds
    }

    private static func listenerPIDs(_ ports: [Int]) -> [Int: [String]] {
        let wanted = ports.sorted()
        guard !wanted.isEmpty else { return [:] }
        let key = wanted.map(String.init).joined(separator: ",")
        listenerLock.lock()
        if listenerCache.key == key, Date().timeIntervalSince(listenerCache.at) < 3 {
            let cached = listenerCache.value
            listenerLock.unlock()
            return cached
        }
        listenerLock.unlock()

        guard let lsof = lsofPath() else { return [:] }
        var listeners: [Int: [String]] = [:]
        for port in wanted {
            guard let out = runTool(lsof, ["-nP", "-iTCP:\(port)",
                                           "-sTCP:LISTEN", "-Fp"]) else { continue }
            let pids = out.split(separator: "\n").compactMap { line -> String? in
                guard line.first == "p" else { return nil }
                let pid = line.dropFirst()
                return pid.allSatisfy(\.isNumber) ? String(pid) : nil
            }
            if !pids.isEmpty { listeners[port] = Array(Set(pids)) }
        }
        listenerLock.lock()
        listenerCache = (Date(), key, listeners)
        listenerLock.unlock()
        return listeners
    }

    private static func lsofPath() -> String? {
        let candidate = "/usr/sbin/lsof"
        return FileManager.default.isExecutableFile(atPath: candidate) ? candidate : nil
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
