import AppKit
import SwiftUI

// How the printer is reached and configured. Reachability lives in the Links
// section; the frontend choice, config sources, data folder and CLI path live
// in Configuration.

struct LinksBlock: View {
    @ObservedObject var model: AppModel
    /// Hoisted by the page: Net.lanIP() opens a socket per call.
    let lanIP: String?

    private var env: KEnvironment { model.env }

    var body: some View {
        PanelSection("Links") {
            HStack(spacing: 8) {
                LinkChip(model: model, label: "Web UI", url: env.webURL)
                if model.snapshot.anyUp {
                    LinkChip(model: model, label: "Moonraker API", url: env.moonrakerURL)
                }
                if env.lanEnabled, let ip = lanIP {
                    LinkChip(model: model, label: "LAN UI",
                             url: "http://\(ip):\(env.uiPort(env.uiName))",
                             systemImage: "globe")
                    LinkChip(model: model, label: "LAN API",
                             url: "http://\(ip):\(env.moonrakerPort)",
                             systemImage: "globe")
                }
                Spacer(minLength: 0)
            }

            HStack(spacing: 8) {
                Button(env.uiName == "mainsail" ? "Open Mainsail" : "Open Fluidd") {
                    model.openURL(env.uiURL(env.uiName))
                }
                .buttonStyle(DemoButton(small: true))

                Spacer()

                HStack(spacing: 10) {
                    Text("LAN access")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Palette.textSecondary)
                    DemoSwitch(isOn: env.lanEnabled) {
                        Task { await model.setLAN(!env.lanEnabled) }
                    }
                    .help("Reachable from other devices on your network")
                }
            }

            if env.lanEnabled {
                if lanIP == nil {
                    hint("Enabled — but this Mac has no network address right now.")
                } else {
                    hint("Restart the stack to apply.")
                }
            } else {
                hint("Only this Mac can reach the stack (localhost).")
                if model.snapshot.anyUp {
                    hint("Restart the stack to apply.")
                }
            }
        }
    }

    private func hint(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(Palette.textMuted)
    }
}

struct ConfigSectionBlock: View {
    @ObservedObject var model: AppModel
    private var env: KEnvironment { model.env }

    var body: some View {
        PanelSection("Configuration") {
            if env.installed {
                ConfigRow(label: "Frontend") {
                    DemoSegmented(
                        items: [.init(value: "fluidd", title: "Fluidd"),
                                .init(value: "mainsail", title: "Mainsail")],
                        selected: env.uiName
                    ) { name in
                        if name != env.uiName {
                            Task { await model.switchUI(name) }
                        }
                    }
                }

                ConfigRow(label: "Serial") {
                    MenuSelect(
                        placeholder: "Default — keep current printer.cfg",
                        options: model.serialPorts.map {
                            MenuSelectOption(value: $0.path,
                                             title: "\($0.path)  \($0.label)")
                        },
                        disabled: model.serialPorts.isEmpty
                    ) { path in
                        Task { await model.setSerial(path) }
                    }
                }
                if model.serialPorts.isEmpty {
                    hint("No USB serial devices found.")
                }

                ConfigRow(label: "Preset") {
                    MenuSelect(
                        placeholder: model.presetNames.isEmpty
                            ? "No presets in \(env.presetsDir.path)"
                            : "Default — use current printer.cfg",
                        options: model.presetNames.map {
                            MenuSelectOption(value: $0, title: $0)
                        },
                        disabled: model.presetNames.isEmpty
                    ) { name in
                        Task { await model.usePreset(name) }
                    }
                }

                hint("Serial and preset both write to printer.cfg — restart the stack to apply.")

                if !env.printerConfigured {
                    HStack(spacing: 6) {
                        Image(systemName: "info.circle")
                            .foregroundStyle(Palette.textMuted)
                        Text("No printer.cfg yet — add one to \(env.config.path), or apply a preset above.")
                            .font(.system(size: 12))
                            .foregroundStyle(Palette.textMuted)
                    }
                }
            }

            ConfigRow(label: "Data") {
                HStack(spacing: 8) {
                    PathChip(env.data.path)
                    Button("Browse…") { chooseDataFolder() }
                        .buttonStyle(DemoButton(small: true))
                        .disabled(model.snapshot.anyUp)
                    Button("Finder") { model.reveal(env.data) }
                        .buttonStyle(DemoButton(small: true))
                }
            }
            if model.snapshot.anyUp {
                hint("Stop the stack before moving the data folder.")
            }

            ConfigRow(label: "Klipper CLI") {
                HStack(spacing: 6) {
                    Image(systemName: env.isCLIInstalled ? "checkmark.circle.fill"
                                                         : "exclamationmark.triangle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(env.isCLIInstalled ? Tint.ok : Tint.attention)
                    Text(env.cliURL?.path ?? "not found")
                        .font(.panelMono(11.5))
                        .foregroundStyle(Palette.textSecondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private func hint(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(Palette.textMuted)
    }

    private func chooseDataFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Move data here"
        panel.message = "Move all Klipper data (configs, logs, web) to a new folder"
        panel.directoryURL = env.data
        if panel.runModal() == .OK, let url = panel.url {
            Task { await model.moveData(url) }
        }
    }
}
