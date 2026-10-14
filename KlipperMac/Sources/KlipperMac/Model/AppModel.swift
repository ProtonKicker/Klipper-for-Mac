import AVFoundation
import AppKit
import Foundation

/// Toast state lives on its own so a message appearing or auto-clearing never
/// invalidates the forms (every @Published write on AppModel re-renders all
/// views observing it).
final class Notifier: ObservableObject {
    @Published var message: String?
    @Published var isError = false
}

/// Camera liveness probe: the helper answers on its own port, so a short GET is
/// cheaper and more honest than reading its pid file from the app.
private enum CamProbe {
    static func isUp(port: Int) async -> Bool {
        guard let url = URL(string: "http://localhost:\(port)/healthz") else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.timeoutInterval = 0.7
        guard let (_, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse
        else { return false }
        return (200..<300).contains(http.statusCode)
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var snapshot = StackSnapshot()
    @Published var printer: PrinterStatus?
    @Published var serialPorts: [SerialPort] = []
    @Published var presetNames: [String] = []
    /// nil until the first check answers — the Camera pane reads that as "unknown".
    @Published var camOnline: Bool?
    @Published var busy = false
    /// Selected interface lives here rather than in KEnvironment so a tap is
    /// reflected at once instead of after the CLI round-trip and cache refill.
    @Published var uiName = KEnvironment.shared.uiName

    let notifier = Notifier()

    // streaming sheet (setup / update --apply)
    @Published var showSheet = false
    @Published var sheetTitle = ""
    @Published var sheetLines: [String] = []
    @Published var sheetRunning = false

    @Published var updateLines: [String] = []

    let env = KEnvironment.shared
    private var timer: Timer?
    private var clearTask: Task<Void, Never>?
    private var refreshInFlight = false
    private var refreshQueued = false
    private var observingActive = false
    private var ticks = 0

    func bootstrap() async {
        await refresh()
        startPolling()
        if !observingActive {
            observingActive = true
            let center = NotificationCenter.default
            center.addObserver(forName: NSApplication.didResignActiveNotification,
                               object: nil, queue: .main) { [weak self] _ in
                guard let self else { return }
                Task { @MainActor in self.stopPolling() }
            }
            center.addObserver(forName: NSApplication.didBecomeActiveNotification,
                               object: nil, queue: .main) { [weak self] _ in
                guard let self else { return }
                Task { @MainActor in
                    self.startPolling()
                    await self.refresh()
                }
            }
            // The camera helper is deliberately detached (a print's monitoring
            // must survive window/terminal churn) — so it also survives app
            // quit. Users read the green camera light as "this app is
            // watching", so quitting must put it out. Synchronous, because
            // the process is about to end; capped, because it must never
            // wedge termination.
            center.addObserver(forName: NSApplication.willTerminateNotification,
                               object: nil, queue: .main) { _ in
                guard let cli = KEnvironment.shared.cliURL else { return }
                let p = Process()
                p.executableURL = cli
                p.arguments = ["cam", "off"]
                p.standardOutput = FileHandle.nullDevice
                p.standardError = FileHandle.nullDevice
                p.standardInput = FileHandle.nullDevice
                do { try p.run() } catch { return }
                let deadline = Date().addingTimeInterval(8)
                while p.isRunning && Date() < deadline { usleep(100_000) }
                if p.isRunning { p.terminate() }
            }
        }
    }

    private func startPolling() {
        guard timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.refresh() }
        }
    }

    private func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    func refresh(reloadEnv: Bool = false) async {
        if reloadEnv { env.invalidateCaches() }
        // A 2s poll is usually in flight when a command finishes or the user
        // hits Refresh. Dropping those requests is what made the UI lag a poll
        // tick behind, so coalesce them into one trailing pass.
        guard !refreshInFlight else { refreshQueued = true; return }
        refreshInFlight = true
        defer { refreshInFlight = false }
        repeat {
            refreshQueued = false
            await refreshOnce()
        } while refreshQueued
    }

    private func refreshOnce() async {
        let env = self.env
        // Presets are only listed in Settings and the scan can block on iCloud;
        // check them every ~10s instead of every tick.
        ticks += 1
        let scanPresets = presetNames.isEmpty || ticks % 5 == 1
        // All filesystem/process work off the main thread — never block UI.
        let (snap, serials, presets) = await Task.detached(priority: .utility) {
            (StackReader.readSync(env: env),
             SerialScanner.scan(),
             scanPresets ? env.presetNames() : nil)
        }.value
        let camPort = env.camPort
        // One cheap GET on the camera port, fired alongside the status fetch so
        // a poll never waits for it twice.
        async let camUp = CamProbe.isUp(port: camPort)
        let printer = snap.anyUp ? await Moonraker.status(port: env.moonrakerPort) : nil
        let cam = await camUp
        if !busy { // don't churn while a command owns the screen
            // Assign only on change: every @Published write re-renders the whole
            // view tree, and most 2s polls change nothing.
            if snap != snapshot { snapshot = snap }
            if printer != self.printer { self.printer = printer }
            if serials != serialPorts { serialPorts = serials }
            if cam != camOnline { camOnline = cam }
            if env.uiName != uiName { uiName = env.uiName }
        }
        if let presets, presets != presetNames { presetNames = presets }
    }

    private func say(_ text: String, error: Bool = false) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        notifier.message = trimmed.isEmpty ? nil : String(trimmed.prefix(400))
        notifier.isError = error
        clearTask?.cancel()
        clearTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 9_000_000_000)
            if !Task.isCancelled { self?.notifier.message = nil }
        }
    }

    // MARK: - CLI-backed actions

    /// The install one-liner lives here so the error toast and the Camera pane
    /// can never drift apart.
    static let installCommand = "curl -fsSL https://raw.githubusercontent.com/"
        + "ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh"

    @discardableResult
    func cli(_ args: [String], ok: String? = nil) async -> Bool {
        busy = true
        env.invalidateCaches()
        let exit: Int32
        do {
            let r = try await CLIRunner.shared.run(args)
            exit = r.exit
            if r.exit == 0 {
                say(ok ?? r.output)
            } else {
                say(r.output.isEmpty ? "klipperformac \(args.first ?? "") failed (exit \(r.exit))"
                                     : r.output, error: true)
            }
        } catch {
            say("Klipper CLI not found — install it first:\n" + Self.installCommand,
                error: true)
            exit = -1
        }
        // Release the screen before refreshing: refreshOnce() skips its writes
        // while busy, so refreshing inside the busy window published nothing.
        busy = false
        await refresh(reloadEnv: true)
        return exit == 0
    }

    func startStack() async { _ = await cli(["up"], ok: "Started") }
    func stopStack() async { _ = await cli(["down"], ok: "Stopped") }
    func restartStack() async { _ = await cli(["restart"], ok: "Restarted") }
    func killAll() async { _ = await cli(["killall"], ok: "Killed everything") }

    /// LAN toggle: no big banner on success — the section itself shows the
    /// state and the "restart to apply" hint. Errors still surface.
    func setLAN(_ on: Bool) async {
        busy = true
        env.invalidateCaches()
        do {
            let r = try await CLIRunner.shared.run(["lan", on ? "on" : "off"])
            if r.exit != 0 {
                say(r.output.isEmpty ? "Could not change LAN access." : r.output, error: true)
            }
        } catch {
            say("Klipper CLI not found.", error: true)
        }
        busy = false
        await refresh(reloadEnv: true)
    }
    func switchUI(_ name: String) async { _ = await cli(["ui", name, "--no-open"], ok: "Switched to \(name.capitalized)") }
    func setSerial(_ path: String) async { _ = await cli(["serial", "--set", path], ok: "Serial device set") }
    func usePreset(_ name: String) async { _ = await cli(["presets", "--use", name], ok: "Preset applied") }
    func moveData(_ dir: URL) async { _ = await cli(["data", "--set", dir.path], ok: "Data folder moved") }

    // MARK: - camera

    /// Failure lines are guidance, never device names — the Camera pane shows
    /// them as a hint instead of a camera option.
    static let camListNoCLI = "Klipper CLI not found — install it first."
    static let camListFailed = "Camera list not ready — press Turn on camera, then Refresh."
    static let camGuidance: Set<String> = [camListNoCLI, camListFailed]

    /// Camera actions ride the same path as up/down so the busy banner, toast and
    /// refresh behave identically. No canned `ok:` text: the CLI's own first line
    /// is the step the beginner has to take next — the one-time "click OK" on the
    /// macOS camera prompt, or the exact file a snapshot just wrote.
    @discardableResult
    func runCam(_ args: [String]) async -> Bool {
        await cli(args)
    }

    /// A bare CLI process cannot surface the macOS camera prompt — the request
    /// hangs unshown and the helper times out. So the app asks first: the
    /// dialog then belongs to "Klipper for Mac", and the helper we spawn
    /// inherits that grant as our child process.
    func startCamera() async {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .notDetermined:
            guard await AVCaptureDevice.requestAccess(for: .video) else {
                say("Camera access declined. To allow it later: System Settings > Privacy & Security > Camera.",
                    error: true)
                return
            }
        case .denied, .restricted:
            say("macOS has blocked camera access for Klipper for Mac. Allow it in System Settings > Privacy & Security > Camera.",
                error: true)
            return
        default:
            break
        }
        await runCam(["cam", "on"])
    }

    func camList() async -> [String] {
        do {
            let r = try await CLIRunner.shared.run(["cam", "list"])
            guard r.exit == 0 else { return [Self.camListFailed] }
            return deviceNames(from: r.output)
        } catch {
            return [Self.camListNoCLI]
        }
    }

    /// The helper prints "index<tab>name<tab>size<tab>fps" per camera. A sentence
    /// (trailing period) is advice from the tool, not a camera — drop it.
    private func deviceNames(from text: String) -> [String] {
        var names: [String] = []
        for raw in text.split(separator: "\n") {
            let fields = raw.split(separator: "\t")
                .map { $0.trimmingCharacters(in: .whitespaces) }
            let name = fields.count > 1 ? fields[1] : (fields.first ?? "")
            if name.isEmpty || name.hasSuffix(".") { continue }
            names.append(name)
        }
        return names
    }

    func pausePrint() async {
        if await Moonraker.post("/printer/print/pause", port: env.moonrakerPort) { say("Paused") }
        await refresh()
    }
    func resumePrint() async {
        if await Moonraker.post("/printer/print/resume", port: env.moonrakerPort) { say("Resumed") }
        await refresh()
    }
    func cancelPrint() async {
        if await Moonraker.post("/printer/print/cancel", port: env.moonrakerPort) { say("Print cancelled") }
        await refresh()
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

    /// Snapshots land in the data folder and the toast disappears after a few
    /// seconds, so the Camera pane needs a standing door to the photos. Before
    /// the first snapshot the folder does not exist yet — show the data folder
    /// instead so the click always lands somewhere real.
    func revealSnapshots() {
        let dir = env.timelapse
        reveal(FileManager.default.fileExists(atPath: dir.path) ? dir : env.data)
    }

    func copyToPasteboard(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}
