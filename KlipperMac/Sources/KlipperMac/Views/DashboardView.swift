import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showLibrary = false

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

    var body: some View {
        Form {
            setupSection
            stackSection
            printerSection
            printerConfigSection
            lanSection
        }
        .formStyle(.grouped)
        .navigationTitle("Printer")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await model.refresh(reloadEnv: true) }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
            }
        }
        .sheet(isPresented: $model.showSheet) {
            SetupSheet(title: model.sheetTitle, lines: model.sheetLines,
                       running: model.sheetRunning)
        }
        .sheet(isPresented: $showLibrary) {
            LibrarySheet()
                .environmentObject(model)
        }
    }

    // MARK: - first run (stack never set up)

    @ViewBuilder private var setupSection: some View {
        if !model.env.installed {
            Section("Setup") {
                Label {
                    Text("Klipper stack is not installed on this Mac")
                } icon: {
                    FlatIcon(kind: .warning, size: 16, color: FlatPalette.amber)
                }
                Text("Installs pristine Klipper, Moonraker, Mainsail and Fluidd from GitHub. Takes a few minutes and needs internet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Button {
                    Task { await model.runSetup() }
                } label: {
                    Label {
                        Text(model.busy ? "Working…" : "Install stack")
                    } icon: {
                        FlatIcon(kind: .download, size: 14, color: FlatPalette.blue)
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.busy)
            }
        }
    }

    // MARK: - stack controls

    private var stackSection: some View {
        Section("Stack") {
            HStack(spacing: 12) {
                StatusBadge(overall: model.snapshot.overall)
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
            }
            if let warning = AppFeedback.inlineWarning(message: model.printer?.stackWarning) {
                Label {
                    Text(warning.message)
                } icon: {
                    FlatIcon(kind: .warning, size: 14, color: FlatPalette.amber)
                }
                .font(.callout)
                .foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Button {
                    Task { await model.startStack() }
                } label: {
                    Text("Start").frame(width: stackButtonWidth)
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.busy || model.snapshot.anyUp || !model.env.installed)

                Button {
                    Task { await model.stopStack() }
                } label: {
                    Text("Stop").frame(width: stackButtonWidth)
                }
                .disabled(model.busy || !model.snapshot.tracked)

                Button {
                    Task { await model.restartStack() }
                } label: {
                    Text("Restart").frame(width: stackButtonWidth)
                }
                .disabled(model.busy || !model.env.installed)

                Button {
                    Task { await model.killAll() }
                } label: {
                    Text("Kill all").frame(width: stackButtonWidth)
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .disabled(model.busy || !model.snapshot.anyUp)
                Spacer()
            }

            if model.snapshot.tracked { serviceRow }

            Picker("Default interface", selection: $model.uiName) {
                Text("Fluidd").tag("fluidd")
                Text("Mainsail").tag("mainsail")
            }
            .pickerStyle(.segmented)
            .onChange(of: model.uiName) { name in
                if name != model.env.uiName {
                    Task { await model.switchUI(name) }
                }
            }

            BrowserLink(label: model.uiName == "mainsail" ? "Mainsail" : "Fluidd",
                        url: model.env.uiURL(model.uiName))
            if model.snapshot.anyUp {
                BrowserLink(label: "Moonraker API", url: model.env.moonrakerURL)
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
            Section("Printer") {
                LabeledContent("Klipper", value: p.connectivityLabel)
                if let note = AppFeedback.inlineWarning(message: p.readinessNote) {
                    Label {
                        Text(note.message)
                    } icon: {
                        FlatIcon(kind: .warning, size: 14, color: FlatPalette.amber)
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
                if let st = p.printState {
                    LabeledContent("State", value: st.capitalized)
                }
                if let file = p.filename, !file.isEmpty {
                    LabeledContent("File", value: file)
                    if let prog = AppFeedback.progressPresentation(p.progress) {
                        VStack(alignment: .leading, spacing: 4) {
                            ProgressView(value: prog.fraction)
                            Text(prog.label)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                if let m = p.message, !m.isEmpty {
                    Text(m)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                if p.printState == "printing" || p.printState == "paused" {
                    HStack {
                        if p.printState == "printing" {
                            Button("Pause") { Task { await model.pausePrint() } }
                        }
                        if p.printState == "paused" {
                            Button("Resume") { Task { await model.resumePrint() } }
                        }
                        Button("Cancel print", role: .destructive) {
                            Task { await model.cancelPrint() }
                        }
                    }
                }
            }
        }
    }

    // MARK: - printer config (serial board + preset)

    @ViewBuilder private var printerConfigSection: some View {
        if model.env.installed {
            Section("Printer config") {
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
                HStack {
                    Button {
                        Task { await model.syncSerial() }
                    } label: {
                        Label("Sync Serial", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .disabled(model.busy)
                    Spacer()
                    Text("Match printer.cfg to the board plugged in")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                LabeledContent("Current config") {
                    Button {
                        showLibrary = true
                    } label: {
                        Text("Change printer config")
                    }
                }

                LabeledContent("Config versions") {
                    Button("Show") { model.showConfigHistoryFolder() }
                }

                Text("Changes write to printer.cfg — restart the stack to apply. "
                     + "The last 10 config versions are saved automatically; "
                     + "press Show to open the folder.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if !model.env.printerConfigured {
                    Label {
                        Text("No printer.cfg yet — press Change printer config, or add one to \(model.env.config.path).")
                    } icon: {
                        FlatIcon(kind: .info, size: 14, color: FlatPalette.amber)
                    }
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: serial device, two ways

    /// The single-board picker: one "Serial device" row that talks to the
    /// non-multi-MCUIT CLI path. The button label is read from model state so
    /// a pick survives the refresh that `setSerial` triggers — that refresh
    /// otherwise reverts the button to "Select device…" for one poll tick.
    private var serialPicker: some View {
        Menu {
            if model.serialPorts.isEmpty {
                Text("No USB serial devices found")
            } else {
                ForEach(model.serialPorts) { port in
                    let owner = claimants[port.path]
                    let claimed = owner != nil
                    Button {
                        Task { await model.setSerial(port.path) }
                    } label: {
                        if claimed, let who = owner {
                            Label("\(port.path)   ·   used by \(who)",
                                  systemImage: "checkmark")
                        } else {
                            Text("\(port.path)   ·   \(portName(port.path))")
                        }
                    }
                }
            }
        } label: {
            Text(serialPickerLabel)
                .foregroundStyle(serialPlaceholder ? .secondary : .primary)
        }
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
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - LAN

    private var lanSection: some View {
        Section {
            Toggle("Reachable from other devices on your network", isOn: Binding(
                get: { model.env.lanEnabled },
                set: { on in Task { await model.setLAN(on) } }
            ))
            if model.env.lanEnabled {
                if let ip = Net.lanIP() {
                    BrowserLink(label: "LAN UI",
                                url: "http://\(ip):\(model.env.uiPort(model.uiName))")
                    BrowserLink(label: "LAN API",
                                url: "http://\(ip):\(model.env.moonrakerPort)")
                    Text("Restart the stack to apply.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Text("Enabled — but this Mac has no network address right now.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("Only this Mac can reach the stack (localhost).")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                if model.snapshot.anyUp {
                    Text("Restart the stack to apply.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("LAN access")
        }
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
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                // Name + badges ride the left; the picker rides the right.
                HStack(spacing: 6) {
                    Text(verbatim: row.displayName)
                        .font(.callout.weight(.medium))
                        .help(row.section)
                    ForEach(Array(row.badgePresentations.enumerated()),
                            id: \.offset) { _, badge in
                        pill(badge)
                    }
                }
                Spacer()
                if row.offersPortPicker {
                    portPicker
                } else if let status = row.pickerStatusPresentation {
                    Text(status.label)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .help(status.help)
                }
            }
            HStack(spacing: 10) {
                // verbatim: a section name or device path is user/CLI content,
                // and Text's default initialiser would read "usb_a_b" as
                // markdown and italicise the middle of it.
                Text(verbatim: row.configuredLine)
                Spacer()
                let identity = row.identityIndicatorPresentation
                if identity.showsIndicator {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(identity.tone == .positive
                                  ? Color.green
                                  : Color.red.opacity(identity.opacity))
                            .frame(width: 7, height: 7)
                        Text(verbatim: identity.text)
                    }
                } else {
                    Text(verbatim: identity.text)
                }
                if let footer = row.footerPresentation {
                    Text(footer.message)
                        .foregroundStyle(footer.tone == .warning
                                         ? FlatPalette.amber : .secondary)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    /// Port menu for THIS section. Candidates come from the scanner the poll
    /// already runs; a configured-but-gone path is not offered (it cannot be
    /// opened), it is shown on the line below instead. A section name the tool
    /// cannot address keeps its row — and loses its write button, because
    /// silently editing [mcu] instead would be the worse failure.
    private var portPicker: some View {
        Menu {
            if ports.isEmpty {
                Text("No USB serial devices found")
            } else {
                ForEach(ports) { port in
                    let path = port.path
                    let owner = claimants[path]
                    let claimedByOther = owner != nil && owner != row.section
                    Button {
                        Task { await model.setSerial(path, mcu: row.mcu) }
                    } label: {
                        if port.path == row.serial {
                            Label("\(port.path)   \(portName(port.path))",
                                  systemImage: "checkmark")
                        } else if claimedByOther, let who = owner {
                            Text("\(port.path)   ·   used by \(who)")
                                .foregroundStyle(.secondary)
                        } else {
                            Text("\(port.path)   ·   \(portName(port.path))")
                        }
                    }
                    .disabled(claimedByOther)
                }
            }
        } label: {
            Text(portPickerButtonText)
                .foregroundStyle(portPlaceholder ? .secondary : .primary)
        }
        .frame(maxWidth: 320)
        .disabled(model.busy || !row.isAddressable || ports.isEmpty)
        .help(row.pickerHelpText)
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

    /// Button label for this section's picker. Source of truth order:
    /// configured serial (from the report) → user's pending pick → suggested
    /// fallback → single-port default → placeholder.
    private var portPickerButtonText: String {
        if let s = row.serial, !s.isEmpty {
            return "\(s)   ·   \(portName(s))"
        }
        if let p = model.pendingSerials[row.mcu], !p.isEmpty {
            return "\(p)   ·   \(portName(p))"
        }
        if let s = row.suggestedPort(in: ports,
                                     allowSinglePortDefault: allowSinglePortDefault)?.path,
           !s.isEmpty {
            return "\(s)   ·   \(portName(s))"
        }
        if ports.count == 1 {
            let p = ports[0]
            return "\(p.path)   ·   \(portName(p.path))"
        }
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
                .font(.caption2.weight(.medium))
        }
        .foregroundStyle(badgeForeground(badge.tone))
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Capsule().fill(badgeBackground(badge.tone)))
        .help(badge.help ?? "")
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
            Button {
                name = ""
                prompt = true
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "plus")
                        .font(.body)
                    Text("Add another board")
                    Spacer()
                }
            }
            .buttonStyle(.borderless)
        }
        .alert("Add another board", isPresented: $prompt) {
            TextField("Section name (e.g. tool, display)", text: $name)
            Button("Cancel", role: .cancel) { name = "" }
            Button("Add") {
                let chosen = name
                name = ""
                Task { await model.addBoard(chosen) }
            }
            .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
        } message: {
            Text("Adds an \"[mcu <name>]\" slot to printer.cfg. Plug the new "
                 + "board in first, then use the new row's dropdown to assign "
                 + "it a port. One word — no ':' or ']'.")
        }
    }
}
