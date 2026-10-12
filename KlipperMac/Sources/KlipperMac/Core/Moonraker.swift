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
        await postJSON(path, body: [:], port: port)
    }

    @discardableResult
    static func postJSON(_ path: String, body: [String: Any], port: Int,
                         timeout: TimeInterval = 8) async -> Bool {
        guard let url = URL(string: "http://localhost:\(port)\(path)") else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = timeout
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["error"] == nil
    }

    static func runScript(_ script: String, port: Int) async -> Bool {
        await postJSON("/printer/gcode/script", body: ["script": script], port: port)
    }
    static func emergencyStop(port: Int) async -> Bool {
        await post("/printer/emergency_stop", port: port)
    }
    static func startPrint(filename: String, port: Int) async -> Bool {
        await postJSON("/printer/print/start", body: ["filename": filename], port: port)
    }

    static func listGcodes(port: Int) async -> [GcodeFile] {
        await listFiles(root: "gcodes", port: port)
    }

    static func listFiles(root: String, port: Int) async -> [GcodeFile] {
        guard let obj = await getJSON("/server/files/list?root=\(root)",
                                      port: port, timeout: 8),
              let arr = (obj["result"] as? [[String: Any]])
        else { return [] }
        return arr.compactMap { f -> GcodeFile? in
            guard let name = (f["path"] as? String) ?? (f["filename"] as? String)
            else { return nil }
            return GcodeFile(
                filename: name,
                size: f["size"] as? Double ?? 0,
                modified: f["modified"] as? Double ?? 0)
        }.sorted { $0.modified > $1.modified }
    }

    static func configFileNames(port: Int) async -> [String] {
        await listFiles(root: "config", port: port).map(\.filename)
    }

    static func fetchText(_ path: String, port: Int) async -> String? {
        guard let url = URL(string: "http://localhost:\(port)\(path)"),
              let (data, _) = try? await URLSession.shared.data(from: url)
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func configFileContent(_ name: String, port: Int) async -> String? {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return await fetchText("/server/files/config/\(encoded)", port: port)
    }

    static func restartKlipper(port: Int) async -> Bool {
        await post("/printer/restart", port: port)
    }

    /// multipart/form-data upload to Moonraker's file manager.
    static func uploadFile(contents: Data, filename: String, root: String,
                           port: Int) async -> Bool {
        guard let url = URL(string: "http://localhost:\(port)/server/files/upload")
        else { return false }
        let boundary = "KlipperMac-\(UUID().uuidString)"
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 120
        req.setValue("multipart/form-data; boundary=\(boundary)",
                     forHTTPHeaderField: "Content-Type")
        var body = Data()
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"root\"\r\n\r\n\(root)\r\n"
                    .data(using: .utf8)!)
        let fileHeader = "--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; "
            + "filename=\"\(filename)\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        body.append(fileHeader.data(using: .utf8)!)
        body.append(contents)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["error"] == nil
    }

    static func gcodeStore(port: Int, count: Int = 100) async -> [ConsoleLine] {
        guard let obj = await getJSON("/server/gcode_store?count=\(count)", port: port),
              let r = obj["result"] as? [String: Any],
              let arr = r["gcode_store"] as? [[String: Any]]
        else { return [] }
        return arr.map { e in
            ConsoleLine(
                text: e["message"] as? String ?? "",
                kind: e["is_error"] as? Bool == true ? .error : .response)
        }
    }

    static func objectList(port: Int) async -> [String] {
        guard let obj = await getJSON("/printer/objects/list", port: port),
              let arr = (obj["result"] as? [String: Any])?["objects"] as? [String]
        else { return [] }
        return arr
    }

    // MARK: history

    static func historyTotals(port: Int) async -> HistoryTotals? {
        guard let obj = await getJSON("/server/history/info", port: port),
              let r = obj["result"] as? [String: Any],
              let t = r["totals"] as? [String: Any]
        else { return nil }
        return HistoryTotals(
            jobs: t["job_count"] as? Int ?? 0,
            totalPrintTime: t["total_duration"] as? Double ?? 0,
            longestPrint: t["longest_print"] as? Double ?? 0,
            filamentUsed: t["total_filament_used"] as? Double ?? 0)
    }

    static func historyList(port: Int, limit: Int = 25) async -> [HistoryJob] {
        guard let obj = await getJSON("/server/history/list?limit=\(limit)", port: port),
              let arr = obj["result"] as? [[String: Any]]
        else { return [] }
        return arr.compactMap { e -> HistoryJob? in
            guard let id = e["job_id"] as? Int else { return nil }
            return HistoryJob(
                id: id,
                filename: e["filename"] as? String ?? "?",
                status: e["status"] as? String ?? "?",
                printDuration: e["print_duration"] as? Double ?? 0,
                totalDuration: e["total_duration"] as? Double ?? 0,
                filamentUsed: e["filament_used"] as? Double ?? 0,
                endDate: e["end_time"] as? Double ?? 0)
        }
    }

    // MARK: system

    static func systemInfo(port: Int) async -> SystemInfo? {
        guard let obj = await getJSON("/machine/system_info", port: port),
              let r = obj["result"] as? [String: Any]
        else { return nil }
        var si = SystemInfo()
        si.hostname = r["hostname"] as? String ?? "?"
        if let cpu = r["cpu_info"] as? [String: Any] {
            si.cpuInfo = cpu["model"] as? String ?? cpu["vendor"] as? String ?? "?"
            si.cpuCount = cpu["cpu_count"] as? Int ?? 0
        }
        if let mem = r["memory"] as? [String: Any] {
            si.memoryTotal = mem["total"] as? Double ?? 0
        }
        if let osd = r["system_info"] as? [String: Any] {
            if let os = osd["os"] as? [String: Any] {
                si.os = os["name"] as? String ?? "?"
            }
            if let kv = osd["kernel_version"] as? String { si.kernelVersion = kv }
            if let dist = osd["distribution"] as? [String: Any] {
                si.distribution = dist["name"] as? String ?? "?"
            }
        }
        return si
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
