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
                .tint(FlatPalette.red)
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
                LabeledContent("Klipper",
                               value: p.connected ? "connected (\(p.klippyState))"
                                                  : "not connected (\(p.klippyState))")
                if let st = p.printState {
                    LabeledContent("State", value: st.capitalized)
                }
                if let file = p.filename, !file.isEmpty {
                    LabeledContent("File", value: file)
                    if let prog = p.progress {
                        VStack(alignment: .leading, spacing: 4) {
                            ProgressView(value: min(max(prog, 0), 1))
                            Text("\(Int((prog * 100).rounded()))%")
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
