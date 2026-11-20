import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showLibrary = false
    @State private var prerequisites = Prerequisites.check()

    /// Resolved product name for a USB path: model-backed identity name wins,
    /// falls back to the scanner's board-agnostic label, else the bare path.
    private func portName(_ path: String) -> String {
        if let n = model.serialPortNames[path], !n.isEmpty { return n }
        if let port = model.serialPorts.first(where: { $0.path == path }) {
            return port.label.isEmpty ? port.path : port.label
        }
        return path
    }

    /// The label a picker button should show for one MCU section. Source of
    /// truth order: (1) configured serial from the CLI report, (2) the user's
    /// still-unconfirmed pick waiting on the next poll, (3) a single attached
    /// board's path as a default, (4) a placeholder. The tuple's Bool is true
    /// when the label is a placeholder (so the view can dim it).
    private func buttonText(forMmcu mcu: String,
                            configured serial: String?,
                            suggested: String?) -> (String, Bool) {
        if let s = serial, !s.isEmpty { return (s, false) }
        if serial == nil || serial!.isEmpty,
           let p = model.pendingSerials[mcu], !p.isEmpty {
            return (p, false)
        }
        if let s = suggested, !s.isEmpty { return (s, false) }
        if model.serialPorts.count == 1 {
            return (model.serialPorts[0].path, false)
        }
        let ph = model.serialPorts.isEmpty ? "No USB serial devices" : "Select device…"
        return (ph, true)
    }

    private let stackButtonWidth: CGFloat = 92

    /// Self-check verdict for the Sync Serial row: every USB MCU the config
    /// claims is there, and its path points at the board it's meant to. nil
    /// when everything matches; a string + tone when a board is missing, on
    /// the wrong port, or shares a port with someone else — text lives beside
    /// the button so the user sees *why* they need to sync.
    private var syncCheckStatus: (text: String, isWarning: Bool)? {
        guard let report = model.mcuReport else { return nil }
        var missingBoard: String?
        var mismatchPort: String?
        var conflictText: String?
        for row in report.mcus {
            // Only USB-over-serial rows are the kind a re-plug can move: CAN
            // rows carry their own uuid and never change path, so they are never
            // "wrong".
            guard row.serial?.hasPrefix("/dev/") == true else { continue }
            if row.conflict == .missingPort {
                missingBoard = row.displayName
                break
            }
            if row.conflict == .duplicatePort {
                conflictText = "port shared with another [mcu]"
                break
            }
            if row.conflict == .badSectionName {
                conflictText = "bad section name"
                break
            }
            if let identity = row.identity, !identity.present {
                // Configured path is there but no identity reply: either the
                // board pulled out or it is on a different /dev/cu.* now.
                mismatchPort = row.displayName
            }
        }
        if let who = missingBoard {
            return ("Board \(who) is not on its configured port — sync to fix", true)
        }
        if let msg = conflictText {
            return ("Cannot sync: \(msg) — edit printer.cfg first", true)
        }
        if let who = mismatchPort {
            return ("Board \(who) not detected at its configured port", true)
        }
        return nil
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if !model.env.installed {
                    Card { setupSection }
                }
                Card { stackSection }
                if model.printer != nil {
                    Card { printerSection }
                }
                if model.env.installed {
                    Card { printerConfigSection }
                }
                Card { lanSection }
            }
            .padding(12)
            .frame(maxWidth: 720, alignment: .leading)
        }
        .sheet(isPresented: $model.showSheet) {
            SetupSheet(title: model.sheetTitle, lines: model.sheetLines,
                       running: model.sheetRunning,
                       onClose: { model.showSheet = false })
        }
        .sheet(isPresented: $showLibrary) {
            LibrarySheet(isPresented: $showLibrary)
                .environmentObject(model)
        }
    }

    // MARK: - first run (stack never set up)

    private var setupSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Setup")
            HStack(spacing: 8) {
                FlatIcon(kind: .warning, size: 16, color: FlatPalette.amber)
                Text("Klipper stack is not installed on this Mac")
            }
            Text("Installs pristine Klipper, Moonraker, Mainsail and Fluidd from GitHub. Takes a few minutes and needs internet. The CLI and its Python runtime are already bundled in this app.")
                .font(.callout)
                .foregroundColor(.secondary)
            Text("Checklist below is a quick preflight; setup performs the authoritative checks.")
                .font(.caption)
                .foregroundColor(.secondary)
            ForEach(prerequisites) { item in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(item.satisfied ? "✓" : "•")
                        .foregroundColor(item.satisfied ? .green : FlatPalette.amber)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.name).font(.callout)
                        Text(item.detail)
                            .font(.caption)
                            .foregroundColor(.secondary)
                        if !item.satisfied && !item.installCommand.isEmpty {
                            HStack(spacing: 6) {
                                Text(item.installCommand)
                                    .font(.system(size: 11, design: .monospaced))
                                Button("Copy") { model.copyToPasteboard(item.installCommand) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                }
            }
            Button(action: {
                Task { await model.runSetup() }
            }) {
                HStack(spacing: 6) {
                    FlatIcon(kind: .download, size: 14, color: FlatPalette.blue)
                    Text(model.busy ? "Working…" : "Install stack")
                }
                .compatBordered()
            }
            .buttonStyle(.plain)
            .disabled(model.busy)
        }
        .onAppear { prerequisites = Prerequisites.check() }
    }

    // MARK: - stack controls

    private var stackSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                sectionHeader("Stack")
                Spacer()
                NoFocusButton(help: "Refresh") {
                    Text("↻").font(.system(size: 15, weight: .medium))
                } action: {
                    Task { await model.refresh(reloadEnv: true) }
                }
            }
            HStack(spacing: 12) {
                StatusBadge(overall: model.snapshot.overall)
                if model.busy { ActivityIndicator() }
                Spacer()
            }
            if let warning = AppFeedback.inlineWarning(message: model.printer?.stackWarning) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    FlatIcon(kind: .warning, size: 14, color: FlatPalette.amber)
                    Text(warning.message)
                }
                .font(.callout)
                .foregroundColor(.secondary)
            }
            HStack(spacing: 8) {
                Button(action: { Task { await model.startStack() } }) {
                    Text("Start")
                        .frame(width: stackButtonWidth)
                        .foregroundColor(.green)
                        .compatBorderedProminent(tint: .green)
                }
                .buttonStyle(.plain)
                .disabled(model.busy || model.snapshot.anyUp || !model.env.installed)

                Button(action: { Task { await model.stopStack() } }) {
                    Text("Stop")
                        .frame(width: stackButtonWidth)
                        .compatBordered()
                }
                .buttonStyle(.plain)
                .disabled(model.busy || !model.snapshot.anyUp)

                Button(action: { Task { await model.restartStack() } }) {
                    Text("Restart")
                        .frame(width: stackButtonWidth)
                        .compatBordered()
                }
                .buttonStyle(.plain)
                .disabled(model.busy || !model.snapshot.anyUp)

                Button(action: { Task { await model.killAll() } }) {
                    Text("Kill all")
                        .frame(width: stackButtonWidth)
                        .foregroundColor(FlatPalette.red)
                        .compatBorderedProminent(tint: .red)
                }
                .buttonStyle(.plain)
                .disabled(model.busy || !model.snapshot.anyUp)
                Spacer()
            }

            if model.snapshot.anyUp { serviceRow }

            if model.snapshot.anyUp {
                Picker("Default interface", selection: Binding(
                    get: { model.uiName },
                    set: { name in
                        model.uiName = name
                        if name != model.env.uiName {
                            Task { await model.switchUI(name) }
                        }
                    }
                )) {
                    Text("Fluidd").tag("fluidd")
                    Text("Mainsail").tag("mainsail")
                }
                .pickerStyle(.segmented)

                BrowserLink(label: model.uiName == "mainsail" ? "Mainsail" : "Fluidd",
                            url: model.env.uiURL(model.uiName))
                BrowserLink(label: "Moonraker API", url: model.env.moonrakerURL)
            } else if model.env.installed {
                Text("Start the stack to open the web interface.")
                    .font(.callout)
                    .foregroundColor(.secondary)
            }
        }
    }

    private var serviceRow: some View {
        HStack(spacing: 18) {
            ForEach(model.snapshot.services.filter { $0.name != "caffeinate" },
                    id: \.name) { s in
                HStack(spacing: 6) {
                    Text(Self.shortName(s.name))
                        .font(.callout)
                    Circle()
                        .fill(s.up ? Color.green : Color.red)
                        .frame(width: 7, height: 7)
                }
            }
            Spacer()
        }
    }

    private static func shortName(_ name: String) -> String {
        switch name {
        case "web_mainsail": return "Mainsail"
        case "web_fluidd": return "Fluidd"
        default: return StackReader.pretty(name)
        }
    }

    // MARK: - live printer status

    @ViewBuilder private var printerSection: some View {
        if let p = model.printer {
            VStack(alignment: .leading, spacing: 8) {
                sectionHeader("Printer")
                FieldRow(label: "Klipper", value: p.connectivityLabel)
                if let note = AppFeedback.inlineWarning(message: p.readinessNote) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        FlatIcon(kind: .warning, size: 14, color: FlatPalette.amber)
                        Text(note.message)
                    }
                    .font(.callout)
                    .foregroundColor(.secondary)
                }
                if let st = p.printState {
                    FieldRow(label: "State", value: st.capitalized)
                }
                if let file = p.filename, !file.isEmpty {
                    FieldRow(label: "File", value: file)
                    if let prog = AppFeedback.progressPresentation(p.progress) {
                        VStack(alignment: .leading, spacing: 4) {
                            CompatProgressBar(value: prog.fraction)
                                .frame(height: 6)
                            Text(prog.label)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
                if let m = p.message, !m.isEmpty {
                    Text(m)
                        .font(.callout)
                        .foregroundColor(.secondary)
                }
                if p.printState == "printing" || p.printState == "paused" {
                    HStack {
                        if p.printState == "printing" {
                            Button("Pause") { Task { await model.pausePrint() } }
                        }
                        if p.printState == "paused" {
                            Button("Resume") { Task { await model.resumePrint() } }
                        }
                        Button("Cancel print") { Task { await model.cancelPrint() } }
                    }
                }
            }
        }
    }

    // MARK: - printer config (serial board + preset)

    @ViewBuilder private var printerConfigSection: some View {
        if model.env.installed {
            VStack(alignment: .leading, spacing: 8) {
                sectionHeader("Printer config")
                // `serial --json` answered: one row per [mcu] section, because
                // the pane has to show what printer.cfg actually holds. An empty
                // list (a config with no MCU section — the contract says that is
                // not a multi-MCU config) and a CLI that has no --json at all
                // both keep the single-board picker exactly as it was: one
                // honest row beats an empty pane, and single-board users must
                // not see a regression.
                if let report = model.mcuReport, !report.isEmpty {
                    mcuRows(report)
                } else {
                    serialPicker
                }

                // "+ Add another board": drops a new ``[mcu <name>]`` stub into
                // printer.cfg via `serial --add-mcu`, then the new row's own port
                // picker assigns the port. A stub from a config that had no
                // slot for this board is how you go from 2 MCUs to 3 (or N).
                AddBoardRow()

                // "Sync Serial": resolves every USB [mcu*] section's port via the
                // board's USB identity (the same VID:PID + serial number macOS
                // rewrites the /dev/cu.* node for on every replug) and rewrites
                // printer.cfg in place. Triggered on demand — not automatic —
                // so cable changes are still the user's call.
                HStack(spacing: 8) {
                    Button(action: { Task { await model.syncSerial() } }) {
                        Text("↻ Sync Serial")
                            .compatBordered()
                    }
                    .buttonStyle(.plain)
                    .disabled(model.busy)
                    if let status = syncCheckStatus {
                        HStack(spacing: 4) {
                            FlatIcon(kind: .warning, size: 14,
                                     color: status.isWarning ? FlatPalette.amber : FlatPalette.green)
                            Text(verbatim: status.text)
                        }
                        .font(.caption)
                        .foregroundColor(.secondary)
                    } else {
                        Text("Match printer.cfg to the board plugged in")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    Spacer()
                }

                LabeledRow(label: "Current config") {
                    Button("Change printer config") {
                        showLibrary = true
                    }
                }

                LabeledRow(label: "Config versions") {
                    Button("Show") { model.showConfigHistoryFolder() }
                }
            }
        }
    }

    // MARK: serial device, two ways

    /// The single-board picker: one "Serial device" row that talks to the
    /// non-multi-MCU CLI path. The button label is read from model state so
    /// a pick survives the refresh that `setSerial` triggers — that refresh
    /// otherwise reverts the button to "Select device…" for one poll tick.
    private var serialPicker: some View {
        Picker(serialPickerLabel, selection: Binding(
            get: { serialConfigured ?? model.pendingSerials[McuRow.masterName] ?? "" },
            set: { path in
                guard !path.isEmpty else { return }
                Task { await model.setSerial(path) }
            }
        )) {
            if model.serialPorts.isEmpty {
                Text("No USB serial devices found").tag("")
            } else {
                ForEach(model.serialPorts) { port in
                    let owner = claimants[port.path]
                    let label = owner.map { "✓ \(port.path)   ·   used by \($0)" }
                        ?? "\(port.path)   ·   \(portName(port.path))"
                    Text(label).tag(port.path)
                }
            }
        }
        .pickerStyle(DefaultPickerStyle())
        .foregroundColor(serialPlaceholder ? .secondary : .primary)
        .disabled(model.busy || model.serialPorts.isEmpty)
    }

    /// Map of configured serial path → owning section (or the last pending pick
    /// if the report hasn't caught up yet).
    private var claimants: [String: String] {
        var map: [String: String] = [:]
        if let report = model.mcuReport {
            for mcu in report.mcus {
                if let s = mcu.serial, s.hasPrefix("/dev/") { map[s] = mcu.section }
            }
        }
        for (mcu, path) in model.pendingSerials {
            if !path.isEmpty, mcu == McuRow.masterName { map[path] = "[mcu]" }
        }
        return map
    }

    private var serialPlaceholder: Bool { model.serialPorts.isEmpty || serialConfigured == nil }

    /// The serial path that currently drives the board configured in printer.cfg,
    /// or nil when none is set. Reads from the CLI report (the master mcu row's
    /// serial), falls back to a pending pick.
    private var serialConfigured: String? {
        if let s = model.mcuReport?
            .mcus.first(where: { $0.mcu == McuRow.masterName })?
            .serial?.trimmingCharacters(in: .whitespacesAndNewlines),
           s.hasPrefix("/dev/") {
            return s
        }
        return model.pendingSerials[McuRow.masterName]
    }

    private var serialPickerLabel: String {
        if let s = serialConfigured, !s.isEmpty {
            return("\(s)   ·   \(portName(s))")
        }

        if model.serialPorts.count == 1 {
            let p = model.serialPorts[0]
            return "\(p.path)   ·   \(portName(p.path))"
        }
        return model.serialPorts.isEmpty ? "No USB serial devices" : "Select device…"
    }
    @ViewBuilder private func mcuRows(_ report: SerialReport) -> some View {
        let singlePickableRow = report.mcus.filter { $0.offersPortPicker }.count == 1
        // Ports that belong to OTHER sections — showing these as live would let a
        // user write the same /dev/cu to two [mcu] sections, which is exactly
        // the conflict that produces the yellow "already claimed" banner. We
        // dim them and tag them "used by [mcu X]" instead.
        let claimants: [String: String] = {
            var map: [String: String] = [:]
            for mcu in report.mcus {
                if let s = mcu.serial, s.hasPrefix("/dev/") {
                    map[s] = mcu.section
                }
            }
            return map
        }()
        ForEach(report.mcus) { row in
            McuSerialRow(row: row,
                         ports: model.serialPorts,
                         allowSinglePortDefault: singlePickableRow,
                         claimants: claimants)
        }
        if !report.single {
            Text("Multiple MCUs: [mcu] keeps the clock and every other board "
                 + "syncs to it, so each one needs a port of its own — the same "
                 + "path twice keeps klipper from starting.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }

    // MARK: - LAN

    private var lanSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader("LAN access")
            Toggle("Reachable from other devices on your network", isOn: Binding(
                get: { model.env.lanEnabled },
                set: { on in Task { await model.setLAN(on) } }
            ))
            .toggleStyle(SwitchToggleStyle())
            if model.env.lanEnabled {
                if let ip = Net.lanIP() {
                    BrowserLink(label: "LAN UI",
                                url: "http://\(ip):\(model.env.uiPort(model.uiName))")
                    BrowserLink(label: "LAN API",
                                url: "http://\(ip):\(model.env.moonrakerPort)")
                    Text("Restart the stack to apply.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                } else {
                    Text("Enabled — but this Mac has no network address right now.")
                        .font(.callout)
                        .foregroundColor(.secondary)
                }
            } else {
                Text("Only this Mac can reach the stack (localhost).")
                    .font(.callout)
                    .foregroundColor(.secondary)
                if model.snapshot.anyUp {
                    Text("Restart the stack to apply.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title).font(.headline)
    }
}

// MARK: - one [mcu] section

/// One MCU of printer.cfg as its own row: what the config claims, whether the
/// board is in front of us right now, and a port picker that addresses ONLY
/// this section. It reads what AppModel already refreshes — no timer of its
/// own, because a pane that polls on its own is how a GUI ends up fighting the
/// CLI over ioreg.
private struct McuSerialRow: View {
    @EnvironmentObject private var model: AppModel
    let row: McuRow
    let ports: [SerialPort]
    let allowSinglePortDefault: Bool
    /// Every serial path currently configured in printer.cfg and the [mcu]
    /// section it belongs to. The picker uses it to grey out ports owned by
    // OTHER sections — letting the user pick them is what triggers the yellow
    // "already claimed" conflict.
    let claimants: [String: String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                HStack(spacing: 6) {
                    Text(verbatim: row.displayName)
                        .font(.system(size: NSFont.systemFontSize, weight: .medium))
                        .helpIfAvailable(row.section)
                    ForEach(Array(row.badgePresentations.enumerated()),
                            id: \.offset) { _, badge in
                        pill(badge)
                    }
                }
                Spacer(minLength: 8)
                if row.offersPortPicker {
                    portPicker
                } else if let status = row.pickerStatusPresentation {
                    Text(status.label)
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .helpIfAvailable(status.help)
                }
            }

            if let path = selectedPortPath {
                VStack(alignment: .leading, spacing: 3) {
                    Text(verbatim: boardName(path))
                        .font(.system(size: NSFont.systemFontSize))
                        .foregroundColor(.primary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, minHeight: 34, alignment: .leading)
                    HStack(spacing: 6) {
                        let identity = row.identityIndicatorPresentation
                        if identity.showsIndicator {
                            Circle()
                                .fill(identity.tone == .positive
                                      ? Color.green
                                      : Color.red.opacity(identity.opacity))
                                .frame(width: 7, height: 7)
                        }
                        Text(verbatim: row.compactIdentityLine)
                        if let usbSerial = row.identity?.serial,
                           !usbSerial.isEmpty,
                           row.serial?.hasPrefix("/dev/") == true {
                            Button(action: { model.copyToPasteboard(usbSerial) }) {
                                Text("⎘").font(.caption)
                            }
                            .buttonStyle(.plain)
                            .helpIfAvailable("Copy USB serial ID: \(usbSerial)\nThe /dev/cu path changes on "
                                  + "every replug — this ID stays with the board so you "
                                  + "can update printer.cfg by hand.")
                        }
                        Spacer(minLength: 0)
                    }
                    .font(.caption)
                    .foregroundColor(.secondary)
                }
                .padding(.leading, 2)
            } else {
                HStack(spacing: 6) {
                    Text(verbatim: row.configuredLine)
                    Spacer(minLength: 0)
                }
                .font(.caption)
                .foregroundColor(.secondary)
                .padding(.leading, 2)
            }

            if let footer = row.footerPresentation {
                Text(footer.message)
                    .font(.caption)
                    .foregroundColor(footer.tone == .warning
                                     ? FlatPalette.amber : .secondary)
                    .padding(.leading, 2)
            }
        }
        .padding(.vertical, 2)
    }

    private var selectedPortPath: String? {
        if let s = row.serial, s.hasPrefix("/dev/") { return s }
        if let p = model.pendingSerials[row.mcu], !p.isEmpty { return p }
        return row.suggestedPort(in: ports,
                                 allowSinglePortDefault: allowSinglePortDefault)?.path
    }

    private func boardName(_ path: String) -> String {
        if let n = model.serialPortNames[path], !n.isEmpty { return n }
        if let n = row.identity?.name, !n.isEmpty { return n }
        if let port = ports.first(where: { $0.path == path }), !port.label.isEmpty {
            return port.label
        }
        return path
    }

    /// Port menu for THIS section. Candidates come from the scanner the poll
    /// already runs; a configured-but-gone path is not offered (it cannot be
    /// opened), it is shown on the line below instead. A section name the tool
    /// cannot address keeps its row — and loses its write button, because
    /// silently editing [mcu] instead would be the worse failure.
    private var portPicker: some View {
        Picker(portPickerButtonText, selection: Binding(
            get: { row.serial ?? model.pendingSerials[row.mcu] ?? "" },
            set: { path in
                guard !path.isEmpty, !claimants.contains(where: { $0.key == path && $0.value != row.section }) else { return }
                Task { await model.setSerial(path, mcu: row.mcu) }
            }
        )) {
            if ports.isEmpty {
                Text("No USB serial devices found").tag("")
            } else {
                ForEach(ports) { port in
                    let owner = claimants[port.path]
                    let claimedByOther = owner != nil && owner != row.section
                    let label = port.path == row.serial
                        ? "✓ \(port.path)   \(portName(port.path))"
                        : (claimedByOther ? "\(port.path)   ·   used by \(owner ?? "another MCU")"
                                          : "\(port.path)   ·   \(portName(port.path))")
                    Text(label).tag(port.path)
                }
            }
        }
        .pickerStyle(DefaultPickerStyle())
        .foregroundColor(portPlaceholder ? .secondary : .primary)
        .frame(maxWidth: 320)
        .disabled(model.busy || !row.isAddressable || ports.isEmpty)
        .helpIfAvailable(row.pickerHelpText)
    }

    /// Resolved picker label for one /dev/cu.* node. The identity "name" (e.g.
    /// "rp2040") is the friendly headline, but two boards of the same class — a
    /// BTT Octopus and an SKR Pico both sit behind rp2040 USB-CDC and both
    /// report identity name "rp2040" — would read identically in the picker with
    /// that alone. The USB serial number is stable per board, so we hang it off
    /// the headline whenever one is available: "rp2040 · 54A1…" vs "rp2040 ·
    /// B2C3…".
    private func portName(_ path: String) -> String {
        var headline = path
        if let n = model.serialPortNames[path], !n.isEmpty {
            headline = n
        } else if let port = ports.first(where: { $0.path == path }) {
            headline = port.label.isEmpty ? path : port.label
        }
        // The serial disambiguates identical-class cards that share the
        // product name + vid:pid. Don't leak it when there is no headline to
        // anchor it to — an orphan serial is a worse label than the path.
        if let s = model.serialPortUSBSerials[path], !s.isEmpty {
            let short = s.count > 8 ? String(s.prefix(8)) + "…" : s
            return "\(headline) · \(short)"
        }
        return headline
    }

    /// The picker button shows only the device path. The long product name
    /// lives in its own two-line block below the row so it cannot stretch the
    /// mcu label or the picker into an unreadable wrapped line.
    private var portPickerButtonText: String {
        if let path = selectedPortPath { return path }
        return ports.isEmpty ? "No USB serial devices" : "Select device…"
    }

    private var portPlaceholder: Bool {
        let configured = (row.serial?.isEmpty == false)
        let pending = (model.pendingSerials[row.mcu]?.isEmpty == false)
        let suggested = row.suggestedPort(in: ports,
                                          allowSinglePortDefault: allowSinglePortDefault) != nil
        return !configured && !pending && !suggested && ports.count != 1
    }

    private func pill(_ badge: RowBadgePresentation) -> some View {
        HStack(spacing: 4) {
            if badge.showsWarningIcon {
                FlatIcon(kind: .warning, size: 12, color: FlatPalette.amber)
            }
            Text(badge.text)
                .font(.caption)
        }
        .foregroundColor(badgeForeground(badge.tone))
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Capsule().fill(badgeBackground(badge.tone)))
        .helpIfAvailable(badge.help ?? "")
    }

    private func badgeForeground(_ tone: RowBadgeTone) -> Color {
        switch tone {
        case .accent:
            return FlatPalette.blue
        case .warning:
            return FlatPalette.amber
        }
    }

    private func badgeBackground(_ tone: RowBadgeTone) -> Color {
        switch tone {
        case .accent:
            return FlatPalette.blue.opacity(0.14)
        case .warning:
            return FlatPalette.amber.opacity(0.14)
        }
    }
}

/// "+ Add another board" button, the companion to the per-section rows above.
/// Drops a new ``[mcu <name>]`` stub into printer.cfg through the
/// `--add-mcu` serial subcommand — so a config can grow from two MCUs to three
/// (or N) without leaving the pane. The new row's own port picker does the
/// port assignment, never this button.
private struct AddBoardRow: View {
    @EnvironmentObject private var model: AppModel
    @State private var prompt = false
    @State private var name = ""

    var body: some View {
        HStack {
            Button(action: {
                name = ""
                prompt = true
            }) {
                HStack(spacing: 4) {
                    Text("+").font(.body)
                    Text("Add another board")
                    Spacer()
                }
            }
            .buttonStyle(.plain)
        }
        .sheet(isPresented: $prompt) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Add another board").font(.headline)
                Text("Adds an \"[mcu <name>]\" slot to printer.cfg. Plug the new "
                     + "board in first, then use the new row's dropdown to assign "
                     + "it a port. One word — no ':' or ']'.")
                    .font(.callout)
                    .foregroundColor(.secondary)
                TextField("Section name (e.g. tool, display)", text: $name)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Spacer()
                    Button("Cancel") {
                        prompt = false
                        name = ""
                    }
                    Button("Add") {
                        let chosen = name
                        prompt = false
                        name = ""
                        Task { await model.addBoard(chosen) }
                    }
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(16)
            .frame(width: 420)
        }
    }
}
