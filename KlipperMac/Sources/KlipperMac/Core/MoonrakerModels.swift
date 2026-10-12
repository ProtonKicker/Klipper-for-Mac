import Foundation

/// Tolerant JSON tree — Moonraker status is partial-diff, Int-or-Double,
/// variable-length arrays; Codable structs fight that shape.
indirect enum JSONValue: Decodable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    private enum DecodingError: Error { case unexpected }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        // Try Double then Int: JSON `60` decodes fine as Double, and a Double
        // is a valid temperature; int-only fields (ids) stay exact via intValue.
        if let d = try? c.decode(Double.self) {
            if d == d.rounded(), abs(d) < 1e15, let i = Int(exactly: d) { self = .int(i) }
            else { self = .double(d) }
            return
        }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([JSONValue].self) { self = .array(a); return }
        if let o = try? c.decode([String: JSONValue].self) { self = .object(o); return }
        throw DecodingError.unexpected
    }

    var doubleValue: Double? {
        switch self {
        case .int(let i): return Double(i)
        case .double(let d): return d
        default: return nil
        }
    }
    var intValue: Int? {
        switch self {
        case .int(let i): return i
        case .double(let d): return Int(exactly: d)
        default: return nil
        }
    }
    var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    var stringValue: String? { if case .string(let s) = self { return s }; return nil }
    var arrayValue: [JSONValue]? { if case .array(let a) = self { return a }; return nil }
    var objectValue: [String: JSONValue]? { if case .object(let o) = self { return o }; return nil }

    subscript(_ key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }
}

// MARK: - Live status model

struct Heater {
    var temp = 0.0
    var target = 0.0
    var power: Double?   // 0…1
}

struct Vec4 {
    var x = 0.0, y = 0.0, z = 0.0, e = 0.0

    init() {}
    init(_ x: Double, _ y: Double, _ z: Double, _ e: Double = 0) {
        self.x = x; self.y = y; self.z = z; self.e = e
    }

    /// Moonraker sends [x, y, z, e] but length varies (3/4/6) with kinematics.
    init?(_ value: JSONValue?) {
        guard let a = value?.arrayValue, a.count >= 3,
              let x = a[0].doubleValue, let y = a[1].doubleValue, let z = a[2].doubleValue
        else { return nil }
        self.x = x; self.y = y; self.z = z
        self.e = a.count > 3 ? a[3].doubleValue ?? 0 : 0
    }
}

enum KlippyState: String {
    case ready, startup, shutdown, disconnected, error
    case connecting = "connecting"
    case unknown
}

struct LivePrinterState {
    var klippy: KlippyState = .unknown

    // print
    var printState: String?
    var filename: String?
    var message: String?
    var progress: Double?
    var printDuration: Double?
    var totalDuration: Double?

    // heaters / fan
    var hotend = Heater()
    var bed = Heater()
    var fan: Double?          // 0…1

    // motion
    var position: Vec4?
    var gcodePosition: Vec4?
    var homedAxes: Set<String> = []
    var speedFactor: Double?  // M220, 0…2+
    var maxVelocity: Double?
    var liveVelocity: Double?

    var saveConfigPending = false

    /// Merged raw status: object -> key -> value. Diff updates merge here at
    /// key level, then typed fields are recomputed — unseen keys are never lost.
    var raw: [String: [String: JSONValue]] = [:]

    // MARK: merge & decode

    mutating func applyStatusDiff(_ diff: [String: [String: JSONValue]]) {
        for (obj, keys) in diff {
            var slot = raw[obj] ?? [:]
            for (k, v) in keys { slot[k] = v }
            raw[obj] = slot
        }
        recompute()
    }

    mutating func applySnapshot(_ snap: [String: [String: JSONValue]]) {
        raw = [:]
        applyStatusDiff(snap)
    }

    mutating func recompute() {
        func heater(_ obj: String) -> Heater {
            guard let o = raw[obj] else { return Heater() }
            return Heater(
                temp: o["temperature"]?.doubleValue ?? 0,
                target: o["target"]?.doubleValue ?? 0,
                power: o["power"]?.doubleValue)
        }
        hotend = heater("extruder")
        bed = heater("heater_bed")
        fan = raw["fan"]?["speed"]?.doubleValue

        if let stats = raw["print_stats"] {
            printState = stats["state"]?.stringValue
            filename = stats["filename"]?.stringValue
            message = stats["message"]?.stringValue
            progress = stats["progress"]?.doubleValue
            printDuration = stats["print_duration"]?.doubleValue
            totalDuration = stats["total_duration"]?.doubleValue
        }
        // display_status / virtual_sdcard push progress too — prefer whichever arrives
        if let p = raw["display_status"]?["progress"]?.doubleValue ?? raw["virtual_sdcard"]?["progress"]?.doubleValue {
            progress = p
        }

        if let th = raw["toolhead"] {
            position = Vec4(th["position"])
            homedAxes = Set(th["homed_axes"]?.arrayValue?.compactMap { $0.stringValue } ?? [])
            maxVelocity = th["max_velocity"]?.doubleValue
        }
        if let gm = raw["gcode_move"] {
            gcodePosition = Vec4(gm["gcode_position"])
            speedFactor = gm["speed_factor"]?.doubleValue
        }
        liveVelocity = raw["motion_report"]?["live_velocity"]?.doubleValue
        saveConfigPending = raw["configfile"]?["save_config_pending"]?.boolValue ?? false
    }

    /// Macro names from configfile settings: skip internal (`_`-prefixed) and
    /// renamed wrappers (rename_ variables or `{rename_` gcode), like Fluidd does.
    var macros: [String] {
        guard let macrosObj = raw["configfile"]?["settings"]?["gcode_macro"]?.objectValue
        else { return [] }
        return macrosObj.compactMap { (name, v) -> String? in
            if name.hasPrefix("_") { return nil }
            if let vars = v["variables"]?.objectValue,
               vars.keys.contains(where: { $0.hasPrefix("rename_") }) { return nil }
            if let g = v["gcode"]?.stringValue, g.contains("{rename_") { return nil }
            return name
        }.sorted()
    }

    var isReady: Bool { klippy == .ready }
}

// MARK: - Console / files

struct ConsoleLine: Identifiable, Equatable {
    enum Kind { case sent, response, error, system }
    let id = UUID()
    let text: String
    let kind: Kind
}

struct GcodeFile: Identifiable, Equatable {
    let filename: String
    let size: Double
    let modified: Double
    var id: String { filename }
}

struct HistoryJob: Identifiable {
    let id: Int
    let filename: String
    let status: String          // completed / cancelled / error / in_progress
    let printDuration: Double
    let totalDuration: Double
    let filamentUsed: Double    // mm
    let endDate: Double
}

struct HistoryTotals {
    var jobs = 0
    var totalPrintTime = 0.0
    var longestPrint = 0.0
    var filamentUsed = 0.0      // mm
}

struct SystemInfo {
    var hostname = "?"
    var cpuInfo = "?"
    var cpuCount = 0
    var memoryTotal = 0.0       // bytes
    var os = "?"
    var kernelVersion = "?"
    var distribution = "?"
    var javaAvailable = false
}
