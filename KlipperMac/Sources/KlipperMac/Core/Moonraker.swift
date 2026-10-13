import Foundation

struct Temp: Equatable {
    var temp: Double
    var target: Double
}

struct Position: Equatable {
    var x: Double
    var y: Double
    var z: Double
}

struct PrinterStatus: Equatable {
    var connected = false
    var klippyState = "?"
    var printState: String?
    var filename: String?
    var progress: Double?
    var message: String?
    var hotend: Temp?
    var bed: Temp?
    var position: Position?
    var printDuration: Double?
    var filamentPresent: Bool?
    var filamentLabel: String?
}

struct TempSample: Equatable, Identifiable {
    var id: Double { time }
    let time: Double
    let hotend: Double
    let hotendTarget: Double
    let bed: Double
    let bedTarget: Double
}

struct GcodeLine: Equatable, Identifiable {
    var id: Double { time }
    let time: Double
    let message: String
    var echoed = false
}

struct WebcamInfo: Equatable {
    var name: String
    var url: String
}

enum Moonraker {
    private static func url(_ path: String, port: Int,
                            items: [URLQueryItem] = []) -> URL? {
        guard var c = URLComponents(string: "http://localhost:\(port)\(path)") else { return nil }
        if !items.isEmpty { c.queryItems = items }
        return c.url
    }

    static func getJSON(_ path: String, port: Int, timeout: TimeInterval = 2,
                        items: [URLQueryItem] = []) async -> [String: Any]? {
        guard let u = url(path, port: port, items: items) else { return nil }
        var req = URLRequest(url: u)
        req.timeoutInterval = timeout
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["error"] == nil
        else { return nil }
        return obj
    }

    @discardableResult
    static func post(_ path: String, port: Int, items: [URLQueryItem] = []) async -> Bool {
        guard let u = url(path, port: port, items: items) else { return false }
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        req.timeoutInterval = 4
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["error"] == nil
    }

    // MARK: - printer objects

    /// Sensor object names are config-dependent (`filament_switch_sensor tool`
    /// etc.), so they have to be discovered before they can be queried.
    static func filamentObjects(port: Int) async -> [String] {
        guard let r = await getJSON("/printer/objects/list", port: port, timeout: 1),
              let objects = (r["result"] as? [String: Any])?["objects"] as? [String]
        else { return [] }
        return objects.filter {
            $0.hasPrefix("filament_switch_sensor ") || $0.hasPrefix("filament_motion_sensor ")
        }
    }

    static func status(port: Int, filamentObjects: [String] = []) async -> PrinterStatus? {
        // Both endpoints are independent — fetch together so a status poll costs
        // one round trip instead of two sequential ones.
        async let infoTask = getJSON("/server/info", port: port)
        var queryItems = [URLQueryItem(name: "print_stats", value: nil),
                          URLQueryItem(name: "extruder", value: nil),
                          URLQueryItem(name: "heater_bed", value: nil),
                          URLQueryItem(name: "gcode_move", value: nil)]
        for obj in filamentObjects { queryItems.append(URLQueryItem(name: obj, value: nil)) }
        let items = queryItems
        async let queryTask = getJSON("/printer/objects/query", port: port,
                                      timeout: 1.5, items: items)
        guard let info = await infoTask, let r = info["result"] as? [String: Any] else { return nil }

        var ps = PrinterStatus(
            connected: r["klippy_connected"] as? Bool ?? false,
            klippyState: r["klippy_state"] as? String ?? "?"
        )

        if let q = await queryTask,
           let st = (q["result"] as? [String: Any])?["status"] as? [String: Any] {
            let stats = st["print_stats"] as? [String: Any]
            ps.printState = stats?["state"] as? String
            ps.filename = stats?["filename"] as? String
            ps.message = stats?["message"] as? String
            if let pr = stats?["progress"] as? Double { ps.progress = pr }
            if let d = stats?["print_duration"] as? Double { ps.printDuration = d }

            func temps(_ key: String) -> Temp? {
                guard let o = st[key] as? [String: Any] else { return nil }
                return Temp(temp: o["temperature"] as? Double ?? 0,
                            target: o["target"] as? Double ?? 0)
            }
            ps.hotend = temps("extruder")
            ps.bed = temps("heater_bed")

            for (key, value) in st where key.contains("filament_") {
                guard let o = value as? [String: Any] else { continue }
                ps.filamentPresent = o["filament_detected"] as? Bool
                ps.filamentLabel = key.split(separator: " ", maxSplits: 1)
                    .last.map(String.init) ?? key
                break
            }

            if let gm = st["gcode_move"] as? [String: Any],
               let pos = gm["gcode_position"] as? [Any], pos.count >= 3 {
                func d(_ i: Int) -> Double {
                    (pos[i] as? Double) ?? Double(pos[i] as? Int ?? 0)
                }
                ps.position = Position(x: d(0), y: d(1), z: d(2))
            }
        }
        return ps
    }

    /// Slicer-estimated total seconds for a file, when metadata exists.
    static func estimatedTime(filename: String, port: Int) async -> Double? {
        guard let r = await getJSON("/server/files/metadata", port: port, timeout: 1.5,
                                    items: [URLQueryItem(name: "filename", value: filename)]),
              let result = r["result"] as? [String: Any]
        else { return nil }
        return result["estimated_time"] as? Double
    }

    // MARK: - console & camera

    static func sendGcode(_ script: String, port: Int) async -> Bool {
        await post("/printer/gcode/script", port: port,
                   items: [URLQueryItem(name: "script", value: script)])
    }

    static func gcodeStore(port: Int, count: Int = 60) async -> [GcodeLine] {
        guard let r = await getJSON("/server/gcode_store", port: port, timeout: 1.5,
                                    items: [URLQueryItem(name: "count", value: String(count))]),
              let result = r["result"] as? [String: Any],
              let rows = result["gcode_store"] as? [[String: Any]]
        else { return [] }
        return rows.compactMap { row in
            guard let message = row["message"] as? String else { return nil }
            let time = (row["time"] as? Double) ?? Double(row["time"] as? Int ?? 0)
            return GcodeLine(time: time,
                             message: message.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }

    static func webcams(port: Int) async -> [WebcamInfo] {
        guard let r = await getJSON("/server/webcam/list", port: port, timeout: 1.5),
              let result = r["result"] as? [String: Any],
              let rows = result["webcams"] as? [[String: Any]]
        else { return [] }
        return rows.compactMap { w in
            let name = (w["name"] as? String) ?? (w["config_name"] as? String) ?? "Camera"
            let u = (w["stream_url"] as? String) ?? (w["snapshot_url"] as? String)
                ?? (w["location"] as? String)
            guard let u, !u.isEmpty else { return nil }
            return WebcamInfo(name: name, url: u)
        }
    }
}
