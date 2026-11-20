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

    var connectivityLabel: String {
        connected ? "connected (\(klippyState))" : "not connected (\(klippyState))"
    }

    var readinessNote: String? {
        let state = klippyState.lowercased()
        if connected && state == "ready" { return nil }
        switch state {
        case "startup":
            return "Klippy is still starting up. The stack can be running before the printer is actually ready."
        case "error":
            let detail = (message ?? "").lowercased()
            if detail.contains("unable to connect") || detail.contains("identify_response") {
                return "Klippy reported an error: the USB board did not answer the Klipper firmware handshake. A connected cable alone is not enough — check that the board has Klipper firmware flashed and that printer.cfg matches that board. See klippy.log."
            }
            return "Klippy reported an error. Check the printer message below or the logs. A USB board can be connected while Klippy waits for a firmware handshake."
        case "shutdown":
            return "Klippy is shut down and needs a restart or firmware recovery."
        default:
            return connected ? "Klippy is connected but not ready." : nil
        }
    }

    var stackWarning: String? {
        guard let note = readinessNote else { return nil }
        return "Printer not ready (\(klippyState)): \(note)"
    }
}

enum Moonraker {
    /// URLSession's async `data(for:)` requires macOS 12+; the completion
    /// handler + continuation form works back to our 10.15 floor.
    private static func data(for request: URLRequest) async -> Data? {
        await withCheckedContinuation { cont in
            let task = URLSession.shared.dataTask(with: request) { data, _, error in
                cont.resume(returning: error == nil ? data : nil)
            }
            task.resume()
        }
    }

    static func getJSON(_ path: String, port: Int, timeout: TimeInterval = 2) async -> [String: Any]? {
        guard let url = URL(string: "http://localhost:\(port)\(path)") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        guard let data = await data(for: req),
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
        guard let data = await data(for: req),
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
