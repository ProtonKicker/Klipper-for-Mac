import AppKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var logService = "klipper"
    @State private var follow = false

    private var env: KEnvironment { model.env }

    var body: some View {
        Form {
            aboutSection
            dataSection
            updatesSection
            logsSection
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
    }

    // MARK: - about

    private var aboutSection: some View {
        Section("About") {
            LabeledContent("App version", value: appVersion)
            LabeledContent("Klipper CLI",
                           value: env.cliURL?.path ?? "not found")
                .foregroundStyle(env.isCLIInstalled ? Color.primary : Color.orange)
            if env.installed {
                let pins = env.pins
                LabeledContent("Klipper", value: pinText(pins, "klipper"))
                LabeledContent("Moonraker", value: pinText(pins, "moonraker"))
                LabeledContent("Mainsail", value: pinText(pins, "mainsail"))
                LabeledContent("Fluidd", value: pinText(pins, "fluidd"))
            }
            HStack {
                Button {
                    model.openURL("https://github.com/ProtonKicker/Klipper-for-Mac")
                } label: {
                    Label("Project on GitHub", systemImage: "link")
                }
                Button("Copy install command") {
                    model.copyToPasteboard("curl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh")
                    model.message = "Install command copied"
                }
            }
        }
    }

    private var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }

    private func pinText(_ pins: [String: String], _ comp: String) -> String {
        let pin = pins[comp] ?? "?"
        let isSHA = pin.count == 40 && pin.allSatisfy { $0.isHexDigit }
        if let sha = pins[comp + "_sha"], !sha.isEmpty {
            return (isSHA ? String(sha.prefix(7)) : pin) + "  \u{00B7}  " + String(sha.prefix(7))
        }
        return isSHA ? String(pin.prefix(7)) + " (commit)" : pin
    }

    // MARK: - data folder

    private var dataSection: some View {
        Section("Data folder") {
            Text(env.data.path)
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
            HStack {
                Button("Change…") { chooseDataFolder() }
                    .disabled(model.snapshot.anyUp)
                Button("Show in Finder") { model.reveal(env.data) }
                Spacer()
            }
            if model.snapshot.anyUp {
                Text("Stop the stack before moving the data folder.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
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

    // MARK: - updates

    private var updatesSection: some View {
        Section("Component updates") {
            HStack {
                Button("Check") { Task { await model.checkUpdates() } }
                    .disabled(model.busy)
                Button("Apply…") { Task { await model.applyUpdates() } }
                    .disabled(model.busy || model.snapshot.anyUp || !env.installed)
                if model.busy && !model.showSheet {
                    ProgressView().controlSize(.small)
                }
                Spacer()
            }
            if !model.updateLines.isEmpty {
                ForEach(Array(model.updateLines.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    // MARK: - logs

    private var logsSection: some View {
        Section("Logs") {
            Picker("Service", selection: $logService) {
                Text("Klipper (klippy.log)").tag("klipper")
                Text("Moonraker (moonraker.log)").tag("moonraker")
            }
            .pickerStyle(.segmented)

            LogView(url: logURL, follow: follow)

            HStack {
                Toggle("Follow new lines", isOn: $follow)
                    .toggleStyle(.switch)
                Spacer()
                Button("Reveal in Finder") { model.reveal(logURL) }
                Button("Copy path") {
                    model.copyToPasteboard(logURL.path)
                    model.message = "Log path copied"
                }
            }
        }
    }

    private var logURL: URL {
        logService == "klipper" ? env.klippyLog : env.moonrakerLog
    }
}
