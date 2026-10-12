import AppKit
import Foundation
import UniformTypeIdentifiers

@MainActor
final class AppModel: ObservableObject {
    @Published var snapshot = StackSnapshot()
    @Published var printer: PrinterStatus?
    @Published var serialPorts: [SerialPort] = []
    @Published var presetNames: [String] = []
    @Published var message: String?
    @Published var messageIsError = false
    @Published var busy = false

    // live dashboard (WebSocket-driven)
    @Published var live = LivePrinterState()
    @Published var wsConnected = false
    @Published var console: [ConsoleLine] = []
    @Published var macros: [String] = []
    @Published var gcodeFiles: [GcodeFile] = []
    @Published var uploading = false

    // config editor (Moonraker file manager — works while Klipper is down)
    @Published var configFiles: [String] = []
    @Published var configName = ""
    @Published var configText = ""
    @Published var configDirty = false

    // history + system (Moonraker REST — work while Klipper is down)
    @Published var historyTotals = HistoryTotals()
    @Published var historyJobs: [HistoryJob] = []
    @Published var systemInfo = SystemInfo()
    @Published var moonrakerVersion = ""
    @Published var klipperVersion = ""

    // streaming sheet (setup / update --apply)
    @Published var showSheet = false
    @Published var sheetTitle = ""
    @Published var sheetLines: [String] = []
    @Published var sheetRunning = false

    @Published var updateLines: [String] = []

    let env = KEnvironment.shared
    let socket = MoonrakerSocket()
    private var timer: Timer?
    private var clearTask: Task<Void, Never>?
    private var refreshInFlight = false
    private var lastAnyUp = false
    private var socketConsumer: Task<Void, Never>?

    func bootstrap() async {
        await refresh()
        if socketConsumer == nil {
            socketConsumer = Task { [weak self] in
                guard let self else { return }
                for await event in self.socket.events() {
                    self.apply(event)
                }
            }
        }
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
                Task { @MainActor in await self.refresh() }
            }
        }
    }

    private func apply(_ event: SocketEvent) {
        switch event {
        case .connection(let ready):
            wsConnected = ready
            if ready {
                Task {
                    await backfillConsole()
                    await refreshFiles()
                    await loadConfigList()
                    await refreshHistory()
                    await refreshSystem()
                }
            }
        case .klippy(let ks):
            live.klippy = ks
            if ks == .ready { Task { await refreshFiles() } }
        case .status(let state):
            live = state
            macros = state.macros
        case .gcodeResponse(let text):
            appendConsole(ConsoleLine(text: text,
                                      kind: text.hasPrefix("!! ") ? .error : .response))
        case .closed:
            wsConnected = false
        }
    }

    private func appendConsole(_ line: ConsoleLine) {
        console.append(line)
        if console.count > 500 { console.removeFirst(console.count - 500) }
    }

    func backfillConsole() async {
        let lines = await Moonraker.gcodeStore(port: env.moonrakerPort, count: 100)
        if !lines.isEmpty { console = lines }
    }

    func refreshFiles() async {
        gcodeFiles = await Moonraker.listGcodes(port: env.moonrakerPort)
    }

    func refreshHistory() async {
        historyTotals = await Moonraker.historyTotals(port: env.moonrakerPort) ?? historyTotals
        historyJobs = await Moonraker.historyList(port: env.moonrakerPort)
    }

    func refreshSystem() async {
        systemInfo = await Moonraker.systemInfo(port: env.moonrakerPort) ?? systemInfo
        if let info = await Moonraker.getJSON("/machine/update/status?refresh=false",
                                              port: env.moonrakerPort),
           let comps = (info["result"] as? [String: Any])?["version_info"] as? [String: Any] {
            moonrakerVersion = ((comps["moonraker"] as? [String: Any])?["version"] as? String) ?? ""
            klipperVersion = ((comps["klipper"] as? [String: Any])?["version"] as? String) ?? ""
        }
    }

    // MARK: - config editor & upload

    func loadConfigList() async {
        let names = await Moonraker.configFileNames(port: env.moonrakerPort)
        guard !names.isEmpty else { return }
        configFiles = names
        if configName.isEmpty || !names.contains(configName) {
            await loadConfig(names.contains("printer.cfg") ? "printer.cfg" : names[0])
        }
    }

    func loadConfig(_ name: String) async {
        guard let text = await Moonraker.configFileContent(name, port: env.moonrakerPort)
        else { say("Could not read \(name)", error: true); return }
        configName = name
        configText = text
        configDirty = false
    }

    func saveConfig() async {
        guard !configName.isEmpty else { return }
        let data = configText.data(using: .utf8) ?? Data()
        if await Moonraker.uploadFile(contents: data, filename: configName,
                                      root: "config", port: env.moonrakerPort) {
            say("Saved \(configName)")
            configDirty = false
        } else {
            say("Could not save \(configName)", error: true)
        }
    }

    func restartKlipper() async {
        if await Moonraker.restartKlipper(port: env.moonrakerPort) {
            say("Klipper restarting…")
        } else {
            say("Restart request failed", error: true)
        }
    }

    func uploadGcode() {
        let panel = NSOpenPanel()
        if let gcode = UTType(filenameExtension: "gcode") {
            panel.allowedContentTypes = [gcode]
        }
        panel.allowsMultipleSelection = false
        panel.message = "Choose a g-code file to upload to the printer queue"
        guard panel.runModal() == .OK, let url = panel.url,
              let data = try? Data(contentsOf: url) else { return }
        uploading = true
        let name = url.lastPathComponent
        Task {
            let ok = await Moonraker.uploadFile(contents: data, filename: name,
                                                root: "gcodes", port: env.moonrakerPort)
            uploading = false
            say(ok ? "Uploaded \(name)" : "Upload of \(name) failed", error: !ok)
            if ok { await refreshFiles() }
        }
    }

    func refresh() async {
        guard !refreshInFlight else { return }
        refreshInFlight = true
        defer { refreshInFlight = false }
        let env = self.env
        // All filesystem/process work off the main thread — never block UI.
        let (snap, serials, presets) = await Task.detached(priority: .utility) {
            (StackReader.readSync(env: env), SerialScanner.scan(), env.presetNames())
        }.value
        let printer = snap.anyUp ? await Moonraker.status(port: env.moonrakerPort) : nil
        if !busy { // don't churn while a command owns the screen
            snapshot = snap
            self.printer = printer
            serialPorts = serials
        }
        presetNames = presets
        // Drive the WebSocket from stack lifecycle: never reconnect against a dead port.
        if snap.anyUp != lastAnyUp {
            lastAnyUp = snap.anyUp
            if snap.anyUp {
                socket.start(port: env.moonrakerPort, origin: env.webURL)
            } else {
                socket.stop()
                wsConnected = false
                live = LivePrinterState()
                macros = []
            }
        }
    }

    private func say(_ text: String, error: Bool = false) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        message = trimmed.isEmpty ? nil : String(trimmed.prefix(400))
        messageIsError = error
        clearTask?.cancel()
        clearTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 9_000_000_000)
            if !Task.isCancelled { self?.message = nil }
        }
    }

    // MARK: - CLI-backed actions

    @discardableResult
    func cli(_ args: [String]) async -> Bool {
        busy = true
        env.invalidateCaches()
        defer { busy = false }
        do {
            let r = try await CLIRunner.shared.run(args)
            if r.exit == 0 {
                say(r.output)
            } else {
                say(r.output.isEmpty ? "klipperformac \(args.first ?? "") failed (exit \(r.exit))"
                                     : r.output, error: true)
            }
            await refresh()
            return r.exit == 0
        } catch {
            say("Klipper CLI not found — install it first:\ncurl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh",
                error: true)
            return false
        }
    }

    func startStack() async { _ = await cli(["up"]) }
    func stopStack() async { _ = await cli(["down"]) }
    func restartStack() async { _ = await cli(["restart"]) }
    func killAll() async { _ = await cli(["killall"]) }

    /// LAN toggle: no big banner on success — the section itself shows the
    /// state and the "restart to apply" hint. Errors still surface.
    func setLAN(_ on: Bool) async {
        busy = true
        env.invalidateCaches()
        defer { busy = false }
        do {
            let r = try await CLIRunner.shared.run(["lan", on ? "on" : "off"])
            if r.exit != 0 {
                say(r.output.isEmpty ? "Could not change LAN access." : r.output, error: true)
            }
            await refresh()
        } catch {
            say("Klipper CLI not found.", error: true)
        }
    }
    func switchUI(_ name: String) async { _ = await cli(["ui", name, "--no-open"]) }
    func setSerial(_ path: String) async { _ = await cli(["serial", "--set", path]) }
    func usePreset(_ name: String) async { _ = await cli(["presets", "--use", name]) }
    func moveData(_ dir: URL) async { _ = await cli(["data", "--set", dir.path]) }

    func pausePrint() async {
        if await Moonraker.post("/printer/print/pause", port: env.moonrakerPort) { say("pause sent") }
        await refresh()
    }
    func resumePrint() async {
        if await Moonraker.post("/printer/print/resume", port: env.moonrakerPort) { say("resume sent") }
        await refresh()
    }
    func cancelPrint() async {
        if await Moonraker.post("/printer/print/cancel", port: env.moonrakerPort) { say("print cancelled") }
        await refresh()
    }

    // MARK: - dashboard actions (gcode via REST, state returns over WebSocket)

    func runGcode(_ script: String) {
        let trimmed = script.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        appendConsole(ConsoleLine(text: "> " + trimmed, kind: .sent))
        Task {
            if !(await Moonraker.runScript(trimmed, port: env.moonrakerPort)) {
                appendConsole(ConsoleLine(text: "!! command rejected", kind: .error))
            }
        }
    }

    func emergencyStop() {
        Task {
            if await Moonraker.emergencyStop(port: env.moonrakerPort) { say("emergency stop sent") }
            else { say("emergency stop failed", error: true) }
            await refresh()
        }
    }

    func startPrint(_ file: GcodeFile) {
        Task {
            if await Moonraker.startPrint(filename: file.filename, port: env.moonrakerPort) {
                say("started \(file.filename)")
            } else {
                say("could not start \(file.filename)", error: true)
            }
        }
    }

    func preheat(extruder: Double?, bed: Double?) {
        var cmds: [String] = []
        if let e = extruder { cmds.append("M104 S\(Int(e))") }
        if let b = bed { cmds.append("M140 S\(Int(b))") }
        runGcode(cmds.joined(separator: "\n"))
    }

    func coolDown() { runGcode("TURN_OFF_HEATERS\nM106 S0") }

    func home(_ axes: String) {
        // axes: "" = all, or any of x y z
        runGcode(axes.isEmpty ? "G28" : "G28 " + axes)
    }

    func jog(x: Double = 0, y: Double = 0) {
        runGcode("G91\nG1 X\(x) Y\(y) F3000\nG90")
    }

    func zMove(_ amount: Double) {
        runGcode("G91\nG1 Z\(amount) F600\nG90")
    }

    func extrude(length: Double, speed: Double) {
        runGcode("M83\nG1 E\(length) F\(speed * 60)")
    }

    func setSpeedFactor(_ percent: Double) {
        runGcode("M220 S\(Int(percent))")
    }

    func checkUpdates() async {
        busy = true
        updateLines = ["Checking upstream tags…"]
        defer { busy = false }
        do {
            let r = try await CLIRunner.shared.run(["update"])
            let lines = r.output
                .split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            updateLines = lines.isEmpty ? ["All components at newest pinned tags."] : lines
        } catch {
            updateLines = ["Klipper CLI not found."]
        }
    }

    func applyUpdates() async {
        if snapshot.anyUp {
            say("Stop the stack before applying updates.", error: true)
            return
        }
        await stream(["update", "--apply"], title: "Applying updates")
        await refresh()
    }

    // MARK: - streaming long jobs (setup / updates)

    func runSetup() async {
        await stream(["setup"], title: "Installing Klipper stack")
        await refresh()
    }

    private func stream(_ args: [String], title: String) async {
        showSheet = true
        sheetTitle = title
        sheetLines = []
        sheetRunning = true
        busy = true
        defer {
            busy = false
            sheetRunning = false
        }
        do {
            let code = try await CLIRunner.shared.stream(args) { line in
                Task { @MainActor in self.sheetLines.append(line) }
            }
            if code != 0 {
                sheetLines.append("—— finished with exit \(code) ——"
                    + (code == 126 ? " (this build's Python is too old; setup needs 3.10+)" : ""))
            }
        } catch {
            sheetLines.append("error: " + error.localizedDescription)
        }
    }

    // MARK: - openers

    func openURL(_ s: String) {
        guard let u = URL(string: s) else { return }
        NSWorkspace.shared.open(u)
    }

    func reveal(_ url: URL) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func copyToPasteboard(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }

    func copyURL(_ s: String) {
        copyToPasteboard(s)
        say("Copied \(s)")
    }
}
