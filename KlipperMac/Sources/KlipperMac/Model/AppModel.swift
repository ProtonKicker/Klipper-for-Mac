import AppKit
import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published var snapshot = StackSnapshot()
    @Published var printer: PrinterStatus?
    @Published var serialPorts: [SerialPort] = []
    @Published var presetNames: [String] = []
    @Published var message: String?
    @Published var messageIsError = false
    @Published var busy = false

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

    func bootstrap() async {
        await refresh()
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
                Task { @MainActor in await self.refresh() }
            }
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
