import AppKit
import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published var snapshot = StackSnapshot()
    @Published var presetNames: [String] = []
    @Published var serialPorts: [SerialPort] = []
    @Published var currentSerial: String?
    @Published var busy = false

    // streaming sheet (setup / update --apply)
    @Published var showSheet = false
    @Published var sheetTitle = ""
    @Published var sheetLines: [String] = []
    @Published var sheetRunning = false

    @Published var updateLines: [String] = []

    let env = KEnvironment.shared
    private var timer: Timer?
    private var refreshInFlight = false
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

    func refresh() async {
        guard !refreshInFlight else { return }
        refreshInFlight = true
        defer { refreshInFlight = false }
        let env = self.env
        // Presets are only listed in Settings and the scan can block on iCloud;
        // check them every ~10s instead of every tick.
        ticks += 1
        let scanPresets = presetNames.isEmpty || ticks % 5 == 1
        // All filesystem/process work off the main thread — never block UI.
        let (snap, presets, serials, cfgSerial) = await Task.detached(priority: .utility) {
            (StackReader.readSync(env: env),
             scanPresets ? env.presetNames() : nil,
             scanPresets ? SerialScanner.scan() : nil,
             env.currentSerialPath())
        }.value
        if !busy { // don't churn while a command owns the screen
            // Assign only on change: every @Published write re-renders the whole
            // view tree, and most 2s polls change nothing.
            if snap != snapshot { snapshot = snap }
        }
        if let presets, presets != presetNames { presetNames = presets }
        if let serials, serials != serialPorts { serialPorts = serials }
        if cfgSerial != currentSerial { currentSerial = cfgSerial }
    }

    // MARK: - CLI-backed actions

    /// Stack actions run silently — the cards on the page are the feedback.
    @discardableResult
    func cli(_ args: [String]) async -> Bool {
        busy = true
        env.invalidateCaches()
        defer { busy = false }
        do {
            let r = try await CLIRunner.shared.run(args)
            await refresh()
            return r.exit == 0
        } catch {
            return false
        }
    }

    func startStack() async { _ = await cli(["up"]) }
    func stopStack() async { _ = await cli(["down"]) }
    func restartStack() async { _ = await cli(["restart"]) }
    func killAll() async { _ = await cli(["killall"]) }

    /// LAN toggle: the card itself shows the state and the "restart to
    /// apply" hint — no banner.
    func setLAN(_ on: Bool) async {
        busy = true
        env.invalidateCaches()
        defer { busy = false }
        _ = try? await CLIRunner.shared.run(["lan", on ? "on" : "off"])
        await refresh()
    }

    func switchUI(_ name: String) async { _ = await cli(["ui", name, "--no-open"]) }
    func usePreset(_ name: String) async { _ = await cli(["presets", "--use", name]) }
    func setSerial(_ path: String) async { _ = await cli(["serial", "--set", path]) }
    func moveData(_ dir: URL) async { _ = await cli(["data", "--set", dir.path]) }

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
        guard !snapshot.anyUp else { return } // button is disabled while running
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
}
