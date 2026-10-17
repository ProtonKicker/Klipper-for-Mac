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
}

enum Moonraker {
    static func getJSON(_ path: String, port: Int, timeout: TimeInterval = 2) async -> [String: Any]? {
        guard let url = URL(string: "http://localhost:\(port)\(path)") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["error"] == nil
        else { return nil }
        return obj
    }

    @discardableResult
    static func post(_ path: String, port: Int) async -> Bool {
        guard let url = URL(string: "http://localhost:\(port)\(path)") else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 4
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["error"] == nil
    }

    static func status(port: Int) async -> PrinterStatus? {
        // Both endpoints are independent — fetch together so a status poll costs
        // one round trip instead of two sequential ones.
        async let infoTask = getJSON("/server/info", port: port)
        async let queryTask = getJSON("/printer/objects/query?print_stats&virtual_sdcard&extruder&heater_bed&gcode_move",
                                      port: port, timeout: 1)
        guard let info = await infoTask else { return nil }
        return status(info: info, query: await queryTask)
    }

    static func status(info: [String: Any], query: [String: Any]?) -> PrinterStatus? {
        guard let r = info["result"] as? [String: Any] else { return nil }
        var ps = PrinterStatus(
            connected: r["klippy_connected"] as? Bool ?? false,
            klippyState: r["klippy_state"] as? String ?? "?"
        )

        if let q = query,
           let st = (q["result"] as? [String: Any])?["status"] as? [String: Any] {
            let stats = st["print_stats"] as? [String: Any]
            let vsd = st["virtual_sdcard"] as? [String: Any]
            ps.printState = stats?["state"] as? String
            ps.filename = stats?["filename"] as? String
            ps.message = stats?["message"] as? String
            if let pr = vsd?["progress"] as? Double { ps.progress = pr }

            func temps(_ key: String) -> Temp? {
                guard let o = st[key] as? [String: Any] else { return nil }
                return Temp(temp: o["temperature"] as? Double ?? 0,
                            target: o["target"] as? Double ?? 0)
            }
            ps.hotend = temps("extruder")
            ps.bed = temps("heater_bed")

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
}
