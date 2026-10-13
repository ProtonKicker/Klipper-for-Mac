import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            setupSection
            stackSection
            printerSection
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

                Button("Kill all", role: .destructive) {
                    Task { await model.killAll() }
                }
                .buttonStyle(.borderedProminent)
                .tint(.orange)
                .disabled(model.busy || !model.snapshot.tracked)
                Spacer()
            }

            if model.snapshot.tracked { serviceGrid }

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

            BrowserLink(label: model.env.uiName == "mainsail" ? "Mainsail" : "Fluidd",
                        url: model.env.webURL)
            if model.snapshot.anyUp {
                BrowserLink(label: "Moonraker API", url: model.env.moonrakerURL)
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
                                url: "http://\(ip):\(model.env.uiPort(model.env.uiName))")
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
