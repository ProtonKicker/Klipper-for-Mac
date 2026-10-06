import Foundation

struct PrinterStatus {
    var connected = false
    var klippyState = "?"
    var printState: String?
    var filename: String?
    var progress: Double?
    var message: String?
    var hotend: (temp: Double, target: Double)?
    var bed: (temp: Double, target: Double)?
    var position: (x: Double, y: Double, z: Double)?
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
        guard let info = await getJSON("/server/info", port: port),
              let r = info["result"] as? [String: Any]
        else { return nil }

        var ps = PrinterStatus(
            connected: r["klippy_connected"] as? Bool ?? false,
            klippyState: r["klippy_state"] as? String ?? "?"
        )

        if let q = await getJSON("/printer/objects/query?print_stats&extruder&heater_bed&gcode_move",
                                 port: port),
           let st = (q["result"] as? [String: Any])?["status"] as? [String: Any] {
            let stats = st["print_stats"] as? [String: Any]
            ps.printState = stats?["state"] as? String
            ps.filename = stats?["filename"] as? String
            ps.message = stats?["message"] as? String
            if let pr = stats?["progress"] as? Double { ps.progress = pr }

            func temps(_ key: String) -> (Double, Double)? {
                guard let o = st[key] as? [String: Any] else { return nil }
                return (o["temperature"] as? Double ?? 0, o["target"] as? Double ?? 0)
            }
            ps.hotend = temps("extruder")
            ps.bed = temps("heater_bed")

            if let gm = st["gcode_move"] as? [String: Any],
               let pos = gm["gcode_position"] as? [Any], pos.count >= 3 {
                func d(_ i: Int) -> Double {
                    (pos[i] as? Double) ?? Double(pos[i] as? Int ?? 0)
                }
                ps.position = (d(0), d(1), d(2))
            }
        }
        return ps
    }
}
