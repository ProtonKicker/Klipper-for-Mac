import SwiftUI

// How the printer is reached and configured. Web UI choice and LAN exposure are
// two halves of one question, so they live in a single section.

struct AccessBlock: View {
    @ObservedObject var model: AppModel
    /// Hoisted by the page: Net.lanIP() opens a socket per call.
    let lanIP: String?

    private var env: KEnvironment { model.env }

    var body: some View {
        Section {
            Picker("Default interface", selection: Binding(
                get: { env.uiName },
                set: { name in
                    if name != env.uiName {
                        Task { await model.switchUI(name) }
                    }
                }
            )) {
                Text("Fluidd").tag("fluidd")
                Text("Mainsail").tag("mainsail")
            }
            .pickerStyle(.segmented)

            HStack(spacing: 8) {
                Button(env.uiName == "mainsail" ? "Mainsail" : "Fluidd") {
                    model.openURL(env.uiURL(env.uiName))
                }
                Text("open in browser")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            CopyableURL(model: model, label: "Local", url: env.webURL)
            if model.snapshot.anyUp {
                CopyableURL(model: model, label: "Moonraker API", url: env.moonrakerURL)
            }

            Toggle("Reachable from other devices on your network", isOn: Binding(
                get: { env.lanEnabled },
                set: { on in Task { await model.setLAN(on) } }
            ))
            if env.lanEnabled {
                if let ip = lanIP {
                    CopyableURL(model: model,
                                label: "LAN UI",
                                url: "http://\(ip):\(env.uiPort(env.uiName))")
                    CopyableURL(model: model,
                                label: "LAN API",
                                url: "http://\(ip):\(env.moonrakerPort)")
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
            Text("Web UI & LAN access")
        }
    }
}

struct ConfigBlock: View {
    @ObservedObject var model: AppModel
    @State private var selectedSerial = ""
    @State private var selectedPreset = ""

    private var env: KEnvironment { model.env }

    var body: some View {
        Section("Printer config") {
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

            Text("Both write to printer.cfg — restart the stack to apply.")
                .font(.caption)
                .foregroundStyle(.secondary)

            if !env.printerConfigured {
                Label("No printer.cfg yet — add one to \(env.config.path), or apply a preset above.",
                      systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
