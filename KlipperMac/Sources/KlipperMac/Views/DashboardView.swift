import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedSerial = ""
    @State private var selectedPreset = ""
    @State private var showKillConfirm = false

    var body: some View {
        Form {
            setupSection
            stackSection
            prestartSection
            webUISection
            lanSection
        }
        .formStyle(.grouped)
        .navigationTitle("Dashboard")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await model.refresh() }
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
        .confirmationDialog("Force kill everything, including strays a normal stop would miss?",
                            isPresented: $showKillConfirm, titleVisibility: .visible) {
            Button("Kill everything", role: .destructive) {
                Task { await model.killAll() }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    // MARK: - first run (stack never set up)

    @ViewBuilder private var setupSection: some View {
        if !model.env.installed {
            Section("Setup") {
                Label("Klipper stack is not installed on this Mac",
                      systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(Color.orange)
                Text("Installs pristine Klipper, Moonraker, Mainsail and Fluidd from GitHub. Takes a few minutes and needs internet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Button {
                    Task { await model.runSetup() }
                } label: {
                    Label(model.busy ? "Working…" : "Install stack",
                          systemImage: "square.and.arrow.down")
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
                Button("Start") {
                    Task { await model.startStack() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.busy || model.snapshot.anyUp || !model.env.installed)

                Button("Stop") {
                    Task { await model.stopStack() }
                }
                .disabled(model.busy || !model.snapshot.tracked)

                Button("Restart") {
                    Task { await model.restartStack() }
                }
                .disabled(model.busy || !model.env.installed)

                Button("Kill all…", role: .destructive) { showKillConfirm = true }
                    .disabled(model.busy || !model.snapshot.tracked)
                Spacer()
            }

            if model.snapshot.tracked { serviceGrid }

            if model.snapshot.anyUp {
                CopyableURL(label: model.env.uiName == "mainsail" ? "Mainsail" : "Fluidd",
                            url: model.env.webURL)
                CopyableURL(label: "Moonraker API", url: model.env.moonrakerURL)
            }
        }
    }

    private var serviceGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())],
                  alignment: .leading, spacing: 6) {
            ForEach(model.snapshot.services, id: \.name) { s in
                HStack(spacing: 6) {
                    Circle()
                        .fill(s.up ? Color.green : Color.red)
                        .frame(width: 8, height: 8)
                    Text(StackReader.pretty(s.name))
                        .font(.callout)
                    Spacer()
                }
            }
        }
    }

    // MARK: - optional pre-start choices (serial + preset)

    @ViewBuilder private var prestartSection: some View {
        if model.env.installed && !model.snapshot.anyUp {
            Section("Before you start (optional)") {
                Picker("Serial device", selection: $selectedSerial) {
                    Text("Default — keep current printer.cfg").tag("")
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

                Picker("Preset config", selection: $selectedPreset) {
                    Text("Default — use current printer.cfg").tag("")
                    ForEach(model.presetNames, id: \.self) { name in
                        Text(name).tag(name)
                    }
                }
                .disabled(model.presetNames.isEmpty)
                .onChange(of: selectedPreset) { value in
                    if !value.isEmpty {
                        let name = value
                        selectedPreset = ""
                        Task { await model.usePreset(name) }
                    }
                }

                Text("Pick a USB board and a saved printer.cfg if you like — otherwise the stack starts with your current config.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if !model.env.printerConfigured {
                    Label("No printer.cfg yet — add one to \(model.env.config.path), or apply a preset above.",
                          systemImage: "info.circle")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - web UI

    private var webUISection: some View {
        Section("Web UI") {
            Picker("Default interface", selection: Binding(
                get: { model.env.uiName },
                set: { name in
                    if name != model.env.uiName {
                        Task { await model.switchUI(name) }
                    }
                }
            )) {
                Text("Fluidd").tag("fluidd")
                Text("Mainsail").tag("mainsail")
            }
            .pickerStyle(.segmented)

            HStack(spacing: 8) {
                if model.env.uiName == "mainsail" {
                    Button("Mainsail") {
                        model.openURL(model.env.uiURL("mainsail"))
                    }
                } else {
                    Button("Fluidd") {
                        model.openURL(model.env.uiURL("fluidd"))
                    }
                }
                Text("open in browser")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            CopyableURL(label: "Local", url: model.env.webURL)
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
                    CopyableURL(label: "LAN UI",
                                url: "http://\(ip):\(model.env.uiPort(model.env.uiName))")
                    CopyableURL(label: "LAN API",
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
