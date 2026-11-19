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
    /// Map of `[mcu]` section → serial path the USER picked this session but the
    /// CLI report has not yet caught up with. A pick writes printer.cfg and the
    /// next `serial --json` lands a refresh tick later; this map is the bridge
    /// that stops the button from reverting to "Select device…" in between.
    /// Keyed by `mcu` (the McuRow identity, e.g. "mcu" or "tool"). The CLI's
    /// report (`McuRow.serial`) is the authority once it arrives — this map is
    /// only consulted when that is still nil/empty. It does NOT track the
    /// configured serial; that stays in the report.
    @Published var pendingSerials: [String: String] = [:]
    /// Map of /dev/cu.* path → resolved product name (from the report identity
    /// "name" field). Lets the picker say "SKR Pico" instead of the board-
    /// agnostic "USB-CDC".
    @Published var serialPortNames: [String: String] = [:]
    /// Map of /dev/cu.* path → USB serial number ("5044506110A45B1C"). Two boards
    /// of the same class (e.g. a BTT Octopus + an SKR Pico, both rp2040 USB-CDC
    /// and both reporting identity name "rp2040") would otherwise read the same
    /// in the picker; the serial is the stable per-board discriminator, so
    /// portName() appends it whenever the name alone is ambiguous.
    @Published var serialPortUSBSerials: [String: String] = [:]
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

    // in-app app update (GitHub releases)
    @Published var appUpdateStatus = ""
    @Published var appUpdateBusy = false

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
        // Background check so Settings > About can surface an available app update.
        Task { await checkForAppUpdate() }
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
                _syncNamesFromReport(report)
                _reconcilePendingSerials(report)
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
        notifier.message = AppFeedback.bannerMessage(text)
        notifier.isError = error
        clearTask?.cancel()
        clearTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 9_000_000_000)
            if !Task.isCancelled { self?.notifier.message = nil }
        }
    }

    /// Build serialPortNames from the report identity so picker buttons can
    /// show the real product name ("BigTreeTech Octopus") instead of the
    /// generic USB-CDC label.
private func _syncNamesFromReport(_ report: SerialReport) {
    for mcu in report.mcus {
        guard let serial = mcu.serial, serial.hasPrefix("/dev/") else { continue }
        let name = mcu.identity?.name?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let name, !name.isEmpty {
            serialPortNames[serial] = name
        }
        let usbSerial = mcu.identity?.serial?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !usbSerial.isEmpty {
            serialPortUSBSerials[serial] = usbSerial
        }
    }
}

    /// Drop pending picks the report now confirms — the write landed.
    private func _reconcilePendingSerials(_ report: SerialReport) {
        let confirmed = Set(report.mcus.compactMap { $0.serial })
        for (mcu, path) in pendingSerials {
            if confirmed.contains(path) { pendingSerials[mcu] = nil }
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
                                     notFound: AppFeedback.cliNotFoundInstallMessage(
                                        Self.installCommand)),
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
    /// Add a brand-new ``[mcu <name>]`` slot to printer.cfg — for boards the
    /// user just plugged in that the current config had no section for yet.
    /// The stub gets no serial: the new row's own port picker fills it, so a
    /// half-written port never lands on the wrong node. `--json` style feedback
    /// for the toast comes straight back from the CLI.
    func addBoard(_ name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            say("A board is nothing without a name — try that again.", error: true)
            return
        }
        guard SerialReportDecoder.isSafeArgument(trimmed) else {
            say("Refusing to add [mcu \(trimmed)] — that section name is not one "
                + "the tool can address: a single word, no ':' or ']'. Fix it "
                + "and try again.", error: true)
            return
        }
        _ = await cliShort(["serial", "--add-mcu", trimmed],
                           ok: "Added [mcu \(trimmed)]")
    }
    /// Resolves every USB [mcu*] section's port via USB identity matching
    /// (`serial --sync`): boards plugged in under a new /dev/cu.* get their
    /// serial: line rewritten, single-candidate fallbacks fill the rest. No
    /// input required beyond this one tap.
    func syncSerial() async {
        _ = await cliShort(["serial", "--sync"],
                           ok: "Serial ports synced")
    }
    func setSerial(_ path: String, mcu: String? = nil) async {
        var args = ["serial", "--set", path]
        let target = mcu?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let target, !target.isEmpty else {
            pendingSerials[McuRow.masterName] = path
            _ = await cli(args, ok: "Serial device set")
            return
        }
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
        pendingSerials[target] = path
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
            let first = AppFeedback.firstLine(r.output)
            if good {
                toast = ok ?? (first.isEmpty ? "Done" : first)
            } else {
                toast = first.isEmpty ? "klipperformac \(args.first ?? "") failed." : first
            }
        } catch {
            toast = Self.cliErrorMessage(error,
                                         notFound: AppFeedback.cliNotFoundInstallMessage(
                                            Self.installCommand))
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
            let first = AppFeedback.firstLine(r.output)
            toast = ok ? AppFeedback.camToast(action: args.first ?? "", output: r.output)
                       : (first.isEmpty ? "Camera command failed." : first)
        } catch {
            toast = Self.cliErrorMessage(error, notFound: CameraFeedback.listNoCLI)
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

    /// A bare CLI process cannot surface the macOS camera prompt — the request
    /// hangs unshown and the helper times out. So the app asks first: the
    /// dialog then belongs to "Klipper for Mac", and the helper we spawn
    /// inherits that grant as our child process.
    func startCamera() async {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .notDetermined:
            let granted = await withCheckedContinuation { cont in
                AVCaptureDevice.requestAccess(for: .video) { ok in
                    cont.resume(returning: ok)
                }
            }
            guard granted else {
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
            guard r.exit == 0 else { return [CameraFeedback.listFailed] }
            return CameraFeedback.deviceNames(fromListOutput: r.output)
        } catch {
            return [Self.cliErrorMessage(error, notFound: CameraFeedback.listNoCLI)]
        }
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

    // MARK: - in-app update (GitHub releases)

    struct GHRelease {
        let tag: String
        let name: String
        let version: String
        let prerelease: Bool
        let assetURL: URL
    }

    @Published var availableAppUpdate: GHRelease?

    var availableAppUpdateLabel: String? {
        guard let r = availableAppUpdate else { return nil }
        let v = r.version.isEmpty ? r.tag : r.version
        return r.prerelease ? "\(v) (pre-release)" : v
    }

    var appUpdateButtonTitle: String {
        availableAppUpdate == nil ? "Check for updates" : "Update and Restart"
    }

    func checkForAppUpdate() async {
        guard !appUpdateBusy else { return }
        appUpdateBusy = true
        defer { appUpdateBusy = false }

        let current = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0"
        appUpdateStatus = "Checking for updates…"
        do {
            guard let releasesURL = URL(string: "https://api.github.com/repos/ProtonKicker/Klipper-for-Mac/releases?per_page=20") else {
                appUpdateStatus = "Could not check for updates."
                return
            }
            var req = URLRequest(url: releasesURL)
            req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
            req.setValue("KlipperForMac-Updater", forHTTPHeaderField: "User-Agent")
            req.timeoutInterval = 20
            let data = try await fetchData(request: req)
            guard let arr = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                appUpdateStatus = "Could not read release info."
                return
            }
            let release = arr.compactMap { parseRelease($0) }.first { isNewer($0.version, than: current) }
            if let release {
                availableAppUpdate = release
                let label = availableAppUpdateLabel ?? release.tag
                appUpdateStatus = "Update available: \(label). Restart to install (stop the Klipper stack first if it’s running)."
            } else {
                availableAppUpdate = nil
                appUpdateStatus = "You're up to date (v\(current))."
            }
        } catch {
            availableAppUpdate = nil
            appUpdateStatus = "Update check failed: \(error.localizedDescription)"
        }
    }

    func installAppUpdate() async {
        guard let release = availableAppUpdate else {
            await checkForAppUpdate()
            return
        }
        guard !appUpdateBusy else { return }
        let stackRunning = StackReader.readSync(env: env).anyUp || snapshot.anyUp
        if stackRunning {
            appUpdateStatus = "Stop the Klipper stack before installing the update (it will restart the app when finished)."
            return
        }
        appUpdateBusy = true
        defer { appUpdateBusy = false }
        do {
            appUpdateStatus = "Downloading \(release.name.isEmpty ? release.tag : release.name)…"
            let zipURL = try await downloadFile(from: release.assetURL)
            defer { try? FileManager.default.removeItem(at: zipURL) }

            appUpdateStatus = "Installing \(release.tag)…"
            try installDownloadedApp(zipURL: zipURL)
            availableAppUpdate = nil
            appUpdateStatus = "Restarting to finish update…"
        } catch {
            appUpdateStatus = "Update failed: \(error.localizedDescription)"
        }
    }

    /// One-click flow: check for an update, then install and restart if available
    /// (only when the Klipper stack is not running).
    func updateApp() async {
        if availableAppUpdate == nil {
            await checkForAppUpdate()
        }
        if availableAppUpdate != nil {
            await installAppUpdate()
        }
    }

    private func parseRelease(_ obj: [String: Any]) -> GHRelease? {
        if obj["draft"] as? Bool == true { return nil }
        guard let tag = obj["tag_name"] as? String else { return nil }
        let name = (obj["name"] as? String) ?? tag
        let prerelease = (obj["prerelease"] as? Bool) ?? false
        let version = tag.hasPrefix("v") || tag.hasPrefix("V") ? String(tag.dropFirst()) : tag
        guard let assets = obj["assets"] as? [[String: Any]] else { return nil }
        for asset in assets {
            guard let aname = asset["name"] as? String, aname == "Klipper-for-Mac.zip",
                  let urlStr = asset["browser_download_url"] as? String,
                  let url = URL(string: urlStr) else { continue }
            return GHRelease(tag: tag, name: name, version: version, prerelease: prerelease, assetURL: url)
        }
        return nil
    }

    private func isNewer(_ remote: String, than local: String) -> Bool {
        func parts(_ s: String) -> [Int] {
            let core = s.split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? s
            let t = (core.hasPrefix("v") || core.hasPrefix("V")) ? String(core.dropFirst()) : core
            return t.split(separator: ".").map { token in Int(token.filter { $0.isNumber }) ?? 0 }
        }
        let a = parts(remote), b = parts(local)
        let n = max(a.count, b.count)
        for i in 0..<n {
            let av = i < a.count ? a[i] : 0
            let bv = i < b.count ? b[i] : 0
            if av != bv { return av > bv }
        }
        return false
    }

    private func fetchData(request: URLRequest) async throws -> Data {
        try await withCheckedThrowingContinuation { cont in
            let task = URLSession.shared.dataTask(with: request) { data, _, error in
                if let error = error {
                    cont.resume(throwing: error)
                } else {
                    cont.resume(returning: data ?? Data())
                }
            }
            task.resume()
        }
    }

    private func downloadFile(from url: URL) async throws -> URL {
        try await withCheckedThrowingContinuation { cont in
            var req = URLRequest(url: url)
            req.setValue("KlipperForMac-Updater", forHTTPHeaderField: "User-Agent")
            req.timeoutInterval = 300
            let task = URLSession.shared.downloadTask(with: req) { tmp, _, error in
                if let error = error {
                    cont.resume(throwing: error)
                    return
                }
                guard let tmp else {
                    cont.resume(throwing: NSError(domain: "AppUpdate", code: 1, userInfo: [NSLocalizedDescriptionKey: "Download failed."]))
                    return
                }
                let dest = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("Klipper-for-Mac-update-\(UUID().uuidString).zip")
                do {
                    try? FileManager.default.removeItem(at: dest)
                    try FileManager.default.moveItem(at: tmp, to: dest)
                    cont.resume(returning: dest)
                } catch {
                    cont.resume(throwing: error)
                }
            }
            task.resume()
        }
    }

    private func installDownloadedApp(zipURL: URL) throws {
        let fm = FileManager.default
        let work = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("kfm-update-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: work, withIntermediateDirectories: true)

        let unzip = Process()
        unzip.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        unzip.arguments = ["-x", "-k", zipURL.path, work.path]
        try unzip.run()
        unzip.waitUntilExit()
        guard unzip.terminationStatus == 0 else {
            throw NSError(domain: "AppUpdate", code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not extract the update."])
        }

        let contents = (try? fm.contentsOfDirectory(at: work, includingPropertiesForKeys: nil)) ?? []
        guard let newApp = contents.first(where: { $0.pathExtension == "app" }) else {
            throw NSError(domain: "AppUpdate", code: 3, userInfo: [NSLocalizedDescriptionKey: "Update package did not contain an app."])
        }

        let dest = Bundle.main.bundleURL
        let staged = dest.deletingLastPathComponent().appendingPathComponent(".Klipper-for-Mac-update.app")
        try? fm.removeItem(at: staged)
        try fm.copyItem(at: newApp, to: staged)
        _ = try? fm.removeItem(at: work)

        // Helper waits for this process to exit, replaces the app bundle, and relaunches it.
        let pid = ProcessInfo.processInfo.processIdentifier
        let script = """
        while kill -0 \(pid) >/dev/null 2>&1; do sleep 0.2; done
        sleep 0.3
        rm -rf '\(dest.path.replacingOccurrences(of: "'", with: "'\\''"))'
        ditto '\(staged.path.replacingOccurrences(of: "'", with: "'\\''"))' '\(dest.path.replacingOccurrences(of: "'", with: "'\\''"))'
        rm -rf '\(staged.path.replacingOccurrences(of: "'", with: "'\\''"))'
        xattr -dr com.apple.quarantine '\(dest.path.replacingOccurrences(of: "'", with: "'\\''"))' >/dev/null 2>&1 || true
        open '\(dest.path.replacingOccurrences(of: "'", with: "'\\''"))'
        """
        let helper = Process()
        helper.executableURL = URL(fileURLWithPath: "/bin/sh")
        helper.arguments = ["-c", script]
        try helper.run()

        // Allow the relaunch quit even if normal close/quit behavior would hide the UI.
        if let appDelegate = NSApp.delegate as? AppDelegate {
            appDelegate.allowTerminateForUpdate = true
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            NSApp.terminate(nil)
        }
    }
}
