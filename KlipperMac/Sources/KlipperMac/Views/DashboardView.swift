import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedSerial = ""
    @State private var selectedPreset = ""
    @State private var prerequisites = Prerequisites.check()

    private let stackButtonWidth: CGFloat = 92

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
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
            .padding(16)
            .frame(maxWidth: 760, alignment: .leading)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - first run (stack never set up)

    private var setupSection: some View {
        VStack(alignment: .leading, spacing: 10) {
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
            Button {
                Task { await model.runSetup() }
            } label: {
                HStack(spacing: 6) {
                    FlatIcon(kind: .download, size: 14, color: FlatPalette.blue)
                    Text(model.busy ? "Working…" : "Install stack")
                }
            }
            .buttonStyle(.bordered)
            .disabled(model.busy)
        }
        .onAppear { prerequisites = Prerequisites.check() }
    }

    // MARK: - stack controls

    private var stackSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                sectionHeader("Stack")
                Spacer()
                Button {
                    Task { await model.refresh(reloadEnv: true) }
                } label: {
                    // Image(systemName:) needs macOS 11+; the stack has its own
                    // FlatIcons, so the refresh glyph is just a typed character.
                    Text("↻").font(.system(size: 15, weight: .medium))
                }
                .buttonStyle(.plain)
                .helpIfAvailable("Refresh")
            }
            HStack(spacing: 12) {
                StatusBadge(overall: model.snapshot.overall)
                if model.busy { ActivityIndicator() }
                Spacer()
            }
            HStack(spacing: 8) {
                Button {
                    Task { await model.startStack() }
                } label: {
                    Text("Start").frame(width: stackButtonWidth)
                }
                .buttonStyle(.bordered)
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
                        .foregroundColor(FlatPalette.red)
                }
                .buttonStyle(.bordered)
                .disabled(model.busy || !model.snapshot.tracked)
                Spacer()
            }

            if model.snapshot.tracked { serviceRow }

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

    private var printerSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Printer")
            if let p = model.printer {
                FieldRow(label: "Klipper",
                         value: p.connected ? "connected (\(p.klippyState))"
                                            : "not connected (\(p.klippyState))")
                if let st = p.printState {
                    FieldRow(label: "State", value: st.capitalized)
                }
                if let file = p.filename, !file.isEmpty {
                    FieldRow(label: "File", value: file)
                    if let prog = p.progress {
                        VStack(alignment: .leading, spacing: 4) {
                            ProgressBar(value: min(max(prog, 0), 1))
                            Text("\(Int((prog * 100).rounded()))%")
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

    private var printerConfigSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Printer config")
            Picker("Serial device", selection: Binding(
                get: { selectedSerial },
                set: { value in
                    if !value.isEmpty {
                        let path = value
                        selectedSerial = ""
                        Task { await model.setSerial(path) }
                    }
                }
            )) {
                Text("Select device…").tag("")
                if model.serialPorts.isEmpty {
                    Text("No USB serial devices found").disabled(true).tag(" ")
                }
                ForEach(model.serialPorts) { port in
                    Text("\(port.path)   \(port.label)").tag(port.path)
                }
            }

            Picker("Preset config", selection: Binding(
                get: { selectedPreset },
                set: { value in
                    if !value.isEmpty {
                        let name = value
                        selectedPreset = ""
                        Task { await model.usePreset(name) }
                    }
                }
            )) {
                Text("Select config…").tag("")
                ForEach(model.presetNames, id: \.self) { name in
                    Text(name).tag(name)
                }
            }
            .disabled(model.presetNames.isEmpty)

            Text("Both write to printer.cfg — restart the stack to apply.")
                .font(.caption)
                .foregroundColor(.secondary)

            if !model.env.printerConfigured {
                HStack(spacing: 6) {
                    FlatIcon(kind: .info, size: 14, color: FlatPalette.amber)
                    Text("No printer.cfg yet — add one to \(model.env.config.path), or apply a preset above.")
                        .font(.callout)
                        .foregroundColor(.secondary)
                }
            }
        }
    }

    // MARK: - LAN

    private var lanSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("LAN access")
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

/// macOS 10.15 has no `ProgressView(value:)`; a tiny NSViewRepresentable
/// wrapper renders the same determinate bar.
struct ProgressBar: NSViewRepresentable {
    let value: Double

    func makeNSView(context: Context) -> NSProgressIndicator {
        let bar = NSProgressIndicator()
        bar.style = .bar
        bar.minValue = 0
        bar.maxValue = 1
        return bar
    }

    func updateNSView(_ bar: NSProgressIndicator, context: Context) {
        bar.doubleValue = min(max(value, 0), 1)
    }
}