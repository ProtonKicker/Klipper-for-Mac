import Foundation

private func expand(_ p: String) -> String { (p as NSString).expandingTildeInPath }

/// Mirrors klipperformac/paths.py resolution order, read-only.
/// All file-touching values are served from a short-TTL cache so SwiftUI view
/// bodies never block on I/O (~/Documents can be slow under iCloud Drive).
final class KEnvironment {
    static let shared = KEnvironment()
    private let fm = FileManager.default

    let appHome: URL
    let webPort: Int
    let fluiddPort: Int
    let moonrakerPort: Int

    private init() {
        let e = ProcessInfo.processInfo.environment
        appHome = URL(fileURLWithPath: expand(e["KLIPPERFORMAC_HOME"] ?? "~/.klipperformac"))
        webPort = Int(e["KLIPPERFORMAC_WEB_PORT"] ?? "") ?? 8080
        fluiddPort = Int(e["KLIPPERFORMAC_FLUIDD_PORT"] ?? "") ?? 8081
        moonrakerPort = Int(e["KLIPPERFORMAC_MOONRAKER_PORT"] ?? "") ?? 7125
    }

    // MARK: - cache

    private struct Cache {
        var at = Date.distantPast
        var settings: [String: Any] = [:]
        var pins: [String: String] = [:]
        var data: URL
        var installed = false
        var printerConfigured = false
        var lanEnabled = false
        var uiName = "fluidd"
        var cliURL: URL?

        init(data: URL = URL(fileURLWithPath: "/nonexistent")) { self.data = data }
    }

    private var cache = Cache()
    private let cacheLock = NSLock()
    private let ttl: TimeInterval = 0.5

    func invalidateCaches() {
        cacheLock.lock()
        cache.at = .distantPast
        cacheLock.unlock()
    }

    private func cached() -> Cache {
        cacheLock.lock()
        defer { cacheLock.unlock() }
        if Date().timeIntervalSince(cache.at) >= ttl {
            var c = Cache()
            c.at = Date()
            c.settings = readSettings()
            c.pins = readPins()
            c.data = resolveData(c.settings)
            c.installed = fm.fileExists(atPath: appHome.appendingPathComponent("venv/bin/python").path)
            c.printerConfigured = fm.fileExists(atPath: c.data.appendingPathComponent("config/printer.cfg").path)
            c.lanEnabled = c.settings["lan"] as? Bool ?? false
            c.uiName = c.pins["ui"] == "mainsail" ? "mainsail" : "fluidd"
            c.cliURL = KEnvironment.resolveCLI()
            cache = c
        }
        return cache
    }

    // MARK: - raw readers (only called from cached())

    private func readSettings() -> [String: Any] {
        guard let data = try? Data(contentsOf: appHome.appendingPathComponent("settings.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    private func readPins() -> [String: String] {
        var merged: [String: String] = [
            "klipper": "461c4e3722c3a897fba1c6b3f0780a5315043842",
            "moonraker": "v0.11.0",
            "mainsail": "v2.19.0",
            "fluidd": "v1.37.6",
            "ui": "fluidd",
        ]
        if let data = try? Data(contentsOf: appHome.appendingPathComponent("lockfile.json")),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for (k, v) in obj { merged[k] = "\(v)" }
        }
        return merged
    }

    private func resolveData(_ settings: [String: Any]) -> URL {
        let e = ProcessInfo.processInfo.environment
        if let env = e["KLIPPERFORMAC_DATA"] { return URL(fileURLWithPath: expand(env)) }
        if let s = settings["data"] as? String, !s.isEmpty { return URL(fileURLWithPath: expand(s)) }
        let def = URL(fileURLWithPath: expand("~/Documents/Klipper for Mac"))
        let legacy = URL(fileURLWithPath: expand("~/KlipperData"))
        if !fm.fileExists(atPath: def.path) && fm.fileExists(atPath: legacy.path) { return legacy }
        return def
    }

    static func resolveCLI() -> URL? {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        var dirs = [home.appendingPathComponent(".local/bin"),
                    URL(fileURLWithPath: "/usr/local/bin"),
                    URL(fileURLWithPath: "/opt/homebrew/bin")]
        if let p = ProcessInfo.processInfo.environment["PATH"] {
            dirs += p.split(separator: ":").map { URL(fileURLWithPath: String($0)) }
        }
        for d in dirs {
            let u = d.appendingPathComponent("klipperformac")
            if fm.isExecutableFile(atPath: u.path) { return u }
        }
        return nil
    }

    // MARK: - public (cache-backed)

    var cliURL: URL? { cached().cliURL }
    var isCLIInstalled: Bool { cached().cliURL != nil }

    var settings: [String: Any] { cached().settings }
    var lanEnabled: Bool { cached().lanEnabled }
    var pins: [String: String] { cached().pins }
    var uiName: String { cached().uiName }
    var data: URL { cached().data }
    var installed: Bool { cached().installed }
    var printerConfigured: Bool { cached().printerConfigured }

    var config: URL { data.appendingPathComponent("config") }
    var presetsDir: URL { config.appendingPathComponent("presets") }
    var printerCfg: URL { config.appendingPathComponent("printer.cfg") }
    var run: URL { data.appendingPathComponent("run") }
    var state: URL { run.appendingPathComponent("state.json") }
    var logs: URL { data.appendingPathComponent("logs") }
    var klippyLog: URL { logs.appendingPathComponent("klippy.log") }
    var moonrakerLog: URL { logs.appendingPathComponent("moonraker.log") }
    var venvPy: URL { appHome.appendingPathComponent("venv/bin/python") }
    var klipperDir: URL { appHome.appendingPathComponent("klipper") }

    // MARK: - presets (iCloud-safe: private queue + stuck guard)

    // Directory enumeration under ~/Documents can block indefinitely when
    // iCloud Drive is wedged; run it on a private queue with a timeout so the
    // UI never waits on it, and don't pile up calls while one is stuck.
    private let enumQueue = DispatchQueue(label: "klipperformac.enum")
    private let stuckLock = NSLock()
    private var enumStuck = false

    func presetNames() -> [String] {
        stuckLock.lock()
        let stuck = enumStuck
        stuckLock.unlock()
        if stuck { return [] }
        let sem = DispatchSemaphore(value: 0)
        var out: [String] = []
        enumQueue.async { [fm] in
            if let entries = try? fm.contentsOfDirectory(atPath: self.presetsDir.path) {
                out = entries.filter { $0.hasSuffix(".cfg") }
                    .map { ($0 as NSString).deletingPathExtension }
                    .sorted()
            }
            sem.signal()
        }
        if sem.wait(timeout: .now() + 3) == .timedOut {
            stuckLock.lock()
            enumStuck = true
            stuckLock.unlock()
            return []
        }
        stuckLock.lock()
        enumStuck = false
        stuckLock.unlock()
        return out
    }

    // MARK: - urls

    func uiPort(_ name: String) -> Int { name == "mainsail" ? webPort : fluiddPort }
    func uiURL(_ name: String) -> String { "http://localhost:\(uiPort(name))" }
    var webURL: String { uiURL(uiName) }
    var moonrakerURL: String { "http://localhost:\(moonrakerPort)" }
}
