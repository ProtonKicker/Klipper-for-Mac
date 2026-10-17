import AVFoundation
import AppKit
import CryptoKit
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
    /// One row per [mcu] section, straight from `serial --json`. nil means
    /// "the installed CLI does not answer that contract yet" — every view that
    /// reads it must then show the old single-board picker, unchanged.
    @Published var mcuReport: SerialReport?
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
    /// nil = never asked, false = this CLI has no `serial --json` (stop asking),
    /// true = the contract answers and the MCU pane is live.
    private var serialJSONSupported: Bool?
    /// Set when a refresh arrives while another pass owns the tick, so the
    /// trailing pass still knows the user pressed Refresh.
    private var serialProbeForced = false
    private var observingActive = false
    private var ticks = 0
    private var lastCfgHash: String?
    private var cfgWatch: DispatchSourceFileSystemObject?
    private var pendingSheetLines: [String] = []
    private var sheetFlushTask: Task<Void, Never>?

    private enum CLITimeout {
        static let control: TimeInterval = 20
        static let cameraList: TimeInterval = 10
        static let library: TimeInterval = 15
        static let updateCheck: TimeInterval = 45
        static let snapshot: TimeInterval = 15
        /// Read-only probe on the poll tick: cheap for a human, wasteful for a
        /// loop, so it gets the shortest leash of the bunch. A wedged ioreg must
        /// not hold the MCU pane shut.
        static let serialProbe: TimeInterval = 10
        static let updateApply: TimeInterval = 1_800
        static let setup: TimeInterval = 7_200
    }

    private static func timeout(for args: [String]) -> TimeInterval {
        switch args.first {
        case "setup":
            return CLITimeout.setup
        case "update":
            return args.contains("--apply") ? CLITimeout.updateApply : CLITimeout.updateCheck
        case "presets":
            return args.contains("--library") ? CLITimeout.library : CLITimeout.control
        case "cam":
            return args.contains("list") ? CLITimeout.cameraList : CLITimeout.control
        default:
            return CLITimeout.control
        }
    }

    private static func cliErrorMessage(_ error: Error, notFound: String) -> String {
        if let cli = error as? CLIError, case .notFound = cli {
            return notFound
        }
        return error.localizedDescription
    }

    func bootstrap() async {
        await refresh()
        startPolling()
        startConfigWatch()
        if !observingActive {
            observingActive = true
            let center = NotificationCenter.default
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
        let timer = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in await self.refresh() }
        }
        timer.tolerance = 0.25
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    /// Event-based watcher on the config directory: edits made in TextEdit
    /// must produce a saved version even while the app is backgrounded
    /// (polling stops when inactive). Re-arms after each event because an
    /// atomic save can replace the directory entry itself.
    func startConfigWatch() {
        cfgWatch?.cancel()
        let fd = open(env.config.path, O_EVTONLY)
        guard fd >= 0 else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
                self?.startConfigWatch()
            }
            return
        }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd, eventMask: [.write, .rename, .delete], queue: .main)
        src.setCancelHandler { close(fd) }
        src.setEventHandler { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                await self.checkConfigVersion()
                self.startConfigWatch()
            }
        }
        cfgWatch = src
        src.resume()
    }

    @MainActor
    func checkConfigVersion() async {
        let url = env.printerCfg
        let hash: String? = await Task.detached(priority: .utility) {
            guard let d = try? Data(contentsOf: url) else { return nil }
            return SHA256.hash(data: d).description
        }.value
        guard let hash else { return }
        if lastCfgHash == nil { lastCfgHash = hash; return }
        guard hash != lastCfgHash else { return }
        lastCfgHash = hash
        _ = try? await CLIRunner.shared.run(["presets", "--snapshot", "edited"],
                                            timeout: CLITimeout.snapshot)
    }

    func refresh(reloadEnv: Bool = false) async {
        if reloadEnv { env.invalidateCaches() }
        // A 2s poll is usually in flight when a command finishes or the user
        // hits Refresh. Dropping those requests is what made the UI lag a poll
        // tick behind, so coalesce them into one trailing pass.
        guard !refreshInFlight else {
            refreshQueued = true
            if reloadEnv { serialProbeForced = true }
            return
        }
        refreshInFlight = true
        defer { refreshInFlight = false }
        repeat {
            refreshQueued = false
            let forced = serialProbeForced
            serialProbeForced = false
            await refreshOnce(forceSerial: forced || reloadEnv)
        } while refreshQueued
    }

    private func refreshOnce(forceSerial: Bool) async {
        let env = self.env
        // Presets are only listed in Settings and the scan can block on iCloud;
        // check them every ~10s instead of every tick.
        ticks += 1
        let scanPresets = presetNames.isEmpty || ticks % 5 == 1
        // The MCU list is the same story one tier up in cost: it needs a CLI
        // process, so it rides the ~10s tick (and the passes the user asked
        // for), never the 2s one. Once a CLI says it has no --json we stop
        // asking until a manual Refresh re-probes it — that is also the moment
        // an upgraded CLI becomes visible.
        let probeSerial = forceSerial
            || (serialJSONSupported != false && ticks % 5 == 1)
        // Fired first: the CLI spawn overlaps the filesystem scan below instead
        // of adding to the poll's latency.
        async let mcuProbe = probeSerialReport(allowed: probeSerial)
        // All filesystem/process work off the main thread — never block UI.
        let (snap, serials, presets, cfgHash) = await Task.detached(priority: .utility) {
            () -> (StackSnapshot, [SerialPort], [String]?, String?) in
            let hash: String? = {
                guard let d = try? Data(contentsOf: env.printerCfg) else { return nil }
                return SHA256.hash(data: d).description
            }()
            return (StackReader.readSync(env: env),
                    SerialScanner.scan(),
                    scanPresets ? env.presetNames() : nil,
                    hash)
        }.value
        // Config edited outside the app (TextEdit etc.) -> save a version.
        // The CLI dedups, so our own applies never double-store.
        if let cfgHash {
            if lastCfgHash == nil {
                lastCfgHash = cfgHash
            } else if cfgHash != lastCfgHash {
                lastCfgHash = cfgHash
                Task {
                    _ = try? await CLIRunner.shared.run(["presets", "--snapshot", "edited"],
                                                        timeout: CLITimeout.snapshot)
                }
            }
        }
        let camPort = env.camPort
        // One cheap GET on the camera port, fired alongside the status fetch so
        // a poll never waits for it twice.
        async let camUp = CamProbe.isUp(port: camPort)
        let printer = snap.anyUp ? await Moonraker.status(port: env.moonrakerPort) : nil
        let cam = await camUp
        let mcu = await mcuProbe
        if !busy { // don't churn while a command owns the screen
            // Assign only on change: every @Published write re-renders the whole
            // view tree, and most 2s polls change nothing.
            if snap != snapshot { snapshot = snap }
            if printer != self.printer { self.printer = printer }
            if serials != serialPorts { serialPorts = serials }
            if cam != camOnline { camOnline = cam }
            if env.uiName != uiName { uiName = env.uiName }
            switch mcu {
            case .keep: break // probe skipped or failed: say nothing, change nothing
            case .legacy:
                if mcuReport != nil { mcuReport = nil }
            case .report(let report):
                if report != mcuReport { mcuReport = report }
            }
        }
        if let presets, presets != presetNames { presetNames = presets }
    }

    /// `serial --json` is read-only (exit 0, conflicts as data, no traceback),
    /// so asking is safe — the only cost is a process spawn, which is why the
    /// caller decides when. Three answers: `.keep` (nothing happened, leave the
    /// pane alone), `.legacy` (this CLI has no contract: show the single-board
    /// picker), `.report` (the multi-MCU list).
    private func probeSerialReport(allowed: Bool) async -> SerialProbe {
        guard allowed else { return .keep }
        guard !busy, env.installed, env.isCLIInstalled else { return .keep }
        do {
            let r = try await CLIRunner.shared.run(["serial", "--json"],
                                                   timeout: CLITimeout.serialProbe)
            switch SerialProbePolicy.classify(output: r.output, exitCode: r.exit) {
            case .keep:
                return .keep
            case .legacy:
                serialJSONSupported = false
                return .legacy
            case .report(let report):
                serialJSONSupported = true
                return .report(report)
            }
        } catch {
            // Timeout, or the CLI vanished mid-poll. A failed probe is not a
            // missing board: keep the last honest answer and stay silent — the
            // user has a Refresh button, and the toast belongs to their commands.
            return .keep
        }
    }

    private enum SerialProbe {
        case keep
        case legacy
        case report(SerialReport)
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

    private func enqueueSheetLines(_ lines: [String]) {
        pendingSheetLines.append(contentsOf: lines)
        guard sheetFlushTask == nil else { return }
        sheetFlushTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 80_000_000)
            guard !Task.isCancelled else { return }
            flushPendingSheetLines()
        }
    }

    private func flushPendingSheetLines() {
        sheetFlushTask?.cancel()
        sheetFlushTask = nil
        guard !pendingSheetLines.isEmpty else { return }
        sheetLines.append(contentsOf: pendingSheetLines)
        pendingSheetLines.removeAll(keepingCapacity: true)
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
            let r = try await CLIRunner.shared.run(args, timeout: Self.timeout(for: args))
            exit = r.exit
            if r.exit == 0 {
                say(ok ?? r.output)
            } else {
                say(r.output.isEmpty ? "klipperformac \(args.first ?? "") failed (exit \(r.exit))"
                                     : r.output, error: true)
            }
        } catch {
            say(Self.cliErrorMessage(error,
                                     notFound: "Klipper CLI not found — install it first:\n"
                                     + Self.installCommand),
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
            let r = try await CLIRunner.shared.run(["lan", on ? "on" : "off"],
                                                   timeout: Self.timeout(for: ["lan"]))
            if r.exit != 0 {
                say(r.output.isEmpty ? "Could not change LAN access." : r.output, error: true)
            }
        } catch {
            say(Self.cliErrorMessage(error, notFound: "Klipper CLI not found."), error: true)
        }
        busy = false
        await refresh(reloadEnv: true)
    }
    func switchUI(_ name: String) async { _ = await cli(["ui", name, "--no-open"], ok: "Switched to \(name.capitalized)") }
    /// `serial --set PATH [mcuName]`. The name is only ever sent by the
    /// multi-MCU pane — which exists only when `serial --json` answered — so a
    /// CLI without per-section support keeps getting the exact old two-argument
    /// command from the single-board picker.
    func setSerial(_ path: String, mcu: String? = nil) async {
        var args = ["serial", "--set", path]
        let target = mcu?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let target, !target.isEmpty else {
            _ = await cli(args, ok: "Serial device set")
            return
        }
        // Belt and braces with the row's own refusal: a name the tool cannot
        // address must never be quietly written to the timing master instead.
        guard SerialReportDecoder.isSafeArgument(target) else {
            say("Refusing to write a serial to [mcu \(target)] — that section "
                + "name is not one the tool can address. Fix printer.cfg first.",
                error: true)
            return
        }
        if let row = mcuReport?.mcus.first(where: { $0.mcu == target }),
           !row.offersPortPicker {
            say("Refusing to write a USB serial to \(row.section) — that row is "
                + "configured by a non-USB identity, so edit printer.cfg instead.",
                error: true)
            return
        }
        args.append(target)
        let section = target == McuRow.masterName ? "[mcu]" : "[mcu \(target)]"
        _ = await cli(args, ok: "Serial device set for \(section)")
    }
    func usePreset(_ name: String) async {
        _ = await cliShort(["presets", "--use", name, "--auto-serial"])
    }
    func moveData(_ dir: URL) async { _ = await cli(["data", "--set", dir.path], ok: "Data folder moved") }

    /// Same pipeline as cli() but the toast is one line: the CLI's first
    /// output line (or a canned message), never the full terminal paragraph.
    @discardableResult
    func cliShort(_ args: [String], ok: String? = nil) async -> Bool {
        busy = true
        env.invalidateCaches()
        var good = false
        var toast = ""
        do {
            let r = try await CLIRunner.shared.run(args, timeout: Self.timeout(for: args))
            good = r.exit == 0
            let first = Self.firstLine(r.output)
            if good {
                toast = ok ?? (first.isEmpty ? "Done" : first)
            } else {
                toast = first.isEmpty ? "klipperformac \(args.first ?? "") failed." : first
            }
        } catch {
            toast = Self.cliErrorMessage(error,
                                         notFound: "Klipper CLI not found — install it first:\n"
                                         + Self.installCommand)
        }
        busy = false
        say(toast, error: !good)
        await refresh(reloadEnv: true)
        return good
    }

    /// Upstream example configs (klipper/config), for the Browse sheet.
    func libraryConfigs() async -> [String] {
        do {
            let r = try await CLIRunner.shared.run(["presets", "--library"],
                                                   timeout: Self.timeout(for: ["presets", "--library"]))
            guard r.exit == 0 else { return [] }
            return r.output.split(separator: "\n").map(String.init)
        } catch {
            return []
        }
    }

    func applyLibrary(_ name: String) async {
        _ = await cliShort(["presets", "--use-library", name, "--auto-serial"])
    }

    func showConfigHistoryFolder() {
        let fm = FileManager.default
        do { try fm.createDirectory(at: env.configHistory, withIntermediateDirectories: true) }
        catch { say("Could not open the versions folder.", error: true); return }
        NSWorkspace.shared.activateFileViewerSelecting([env.configHistory])
    }

    // MARK: - camera

    /// Failure lines are guidance, never device names — the Camera pane shows
    /// them as a hint instead of a camera option.
    static let camListNoCLI = "Klipper CLI not found — install it first."
    static let camListFailed = "Camera list not ready — press Turn on camera, then Refresh."
    static let camGuidance: Set<String> = [camListNoCLI, camListFailed]

    /// Camera actions share the busy/refresh behavior of cli(), but the toast
    /// stays to ONE short line: the CLI prints multi-paragraph guidance for the
    /// terminal, which reads as a wall of text in a popup. The two hints that
    /// matter (macOS is asking for permission; a stack restart is needed for
    /// the web UI) get folded into the short line instead of dropped.
    @discardableResult
    func runCam(_ args: [String]) async -> Bool {
        busy = true
        env.invalidateCaches()
        var ok = false
        var toast = ""
        do {
            let r = try await CLIRunner.shared.run(args, timeout: Self.timeout(for: args))
            ok = r.exit == 0
            toast = ok ? Self.camToast(args.first ?? "", output: r.output)
                       : (Self.firstLine(r.output).isEmpty ? "Camera command failed."
                                                           : Self.firstLine(r.output))
        } catch {
            toast = Self.cliErrorMessage(error, notFound: Self.camListNoCLI)
            say(toast, error: true)
            busy = false
            await refresh(reloadEnv: true)
            return false
        }
        busy = false
        say(toast, error: !ok)
        await refresh(reloadEnv: true)
        return ok
    }

    static func firstLine(_ text: String) -> String {
        text.split(separator: "\n").first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? ""
    }

    static func camToast(_ action: String, output: String) -> String {
        let first = firstLine(output)
        switch action {
        case "on":
            if first.contains("ask once") { return "macOS is asking for camera access — click OK." }
            return output.contains("restart")
                ? "Camera is on — restart the stack once to see it in the web UI."
                : "Camera is on"
        case "off":
            return "Camera is off"
        case "snapshot":
            return first  // "Saved: <path>" — short and the point of the action
        case "config":
            return "Saved — turn the camera off and on to apply."
        default:
            return first.isEmpty ? "Done" : first
        }
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
            let r = try await CLIRunner.shared.run(["cam", "list"],
                                                   timeout: Self.timeout(for: ["cam", "list"]))
            guard r.exit == 0 else { return [Self.camListFailed] }
            return deviceNames(from: r.output)
        } catch {
            return [Self.cliErrorMessage(error, notFound: Self.camListNoCLI)]
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
            let r = try await CLIRunner.shared.run(["update"],
                                                   timeout: Self.timeout(for: ["update"]))
            let lines = r.output
                .split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            updateLines = lines.isEmpty ? ["All components at newest pinned tags."] : lines
        } catch {
            updateLines = [Self.cliErrorMessage(error, notFound: "Klipper CLI not found.")]
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
            flushPendingSheetLines()
            busy = false
            sheetRunning = false
        }
        do {
            let code = try await CLIRunner.shared.stream(args,
                                                         timeout: Self.timeout(for: args)) { lines in
                Task { @MainActor in self.enqueueSheetLines(lines) }
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
