import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedSerial = ""
    @State private var showLibrary = false

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
                .disabled(model.busy || !model.snapshot.tracked)
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

    /// The pre-multi-MCU picker, kept verbatim: one "Serial device" row for
    /// every CLI that cannot answer `serial --json`. Single-board users with an
    /// installed build from before the contract see exactly this.
    private var serialPicker: some View {
        Picker("Serial device", selection: $selectedSerial) {
            Text("Select device…").tag("")
            if model.serialPorts.isEmpty {
                Text("No USB serial devices found").disabled(true).tag(" ")
            }
            ForEach(model.serialPorts) { port in
                Text("\(port.path)   \(port.label)").tag(port.path)
            }
        }
        .onChange(of: selectedSerial) { value in
            if !value.isEmpty {
                let path = value
                selectedSerial = ""
                Task { await model.setSerial(path) }
            }
        }
    }

    /// One row per [mcu] section, plus the rule the rows exist to enforce.
    @ViewBuilder private func mcuRows(_ report: SerialReport) -> some View {
        ForEach(report.mcus) { row in
            McuSerialRow(row: row, ports: model.serialPorts)
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
    /// The picker is an action, not a setting: it answers "Select device…"
    /// again as soon as the write is done, exactly like the single-board row.
    @State private var chosen = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(verbatim: row.displayName)
                    .font(.callout.weight(.medium))
                    .help(row.section)
                ForEach(Array(row.badgePresentations.enumerated()), id: \.offset) { _, badge in
                    pill(badge)
                }
                Spacer(minLength: 8)
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
        Picker("Serial device for \(row.displayName)", selection: $chosen) {
            Text("Select device…").tag("")
            if ports.isEmpty {
                Text("No USB serial devices found").disabled(true).tag(" ")
            }
            ForEach(ports) { port in
                Text("\(port.path)   \(port.label)").tag(port.path)
            }
        }
        .labelsHidden()
        .frame(maxWidth: 240)
        .disabled(model.busy || !row.isAddressable)
        .help(row.pickerHelpText)
        .onChange(of: chosen) { value in
            if !value.isEmpty {
                let path = value
                chosen = ""
                Task { await model.setSerial(path, mcu: row.mcu) }
            }
        }
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
