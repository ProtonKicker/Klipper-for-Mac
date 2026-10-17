import AppKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    private var env: KEnvironment { model.env }

    var body: some View {
        Form {
            aboutSection
            componentsSection
            dataSection
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
    }

    // MARK: - about

    private var aboutSection: some View {
        Section("About") {
            LabeledContent("App version", value: appVersion)
            Button {
                model.openURL("https://github.com/ProtonKicker/Klipper-for-Mac")
            } label: {
                Text("Project on GitHub")
            }
        }
    }

    // MARK: - components

    private var componentsSection: some View {
        Section("Components") {
            if env.installed {
                let pins = env.pins
                LabeledContent("Klipper", value: SettingsPresentation.pinText(pins, component: "klipper"))
                LabeledContent("Moonraker", value: SettingsPresentation.pinText(pins, component: "moonraker"))
                LabeledContent("Mainsail", value: SettingsPresentation.pinText(pins, component: "mainsail"))
                LabeledContent("Fluidd", value: SettingsPresentation.pinText(pins, component: "fluidd"))
            }
            HStack {
                Button("Check") { Task { await model.checkUpdates() } }
                    .disabled(model.busy)
                Button("Update") { Task { await model.applyUpdates() } }
                    .disabled(model.busy || model.snapshot.anyUp || !env.installed)
                if model.busy && !model.showSheet {
                    ProgressView().controlSize(.small)
                }
                Spacer()
            }
            if !model.updateLines.isEmpty {
                ForEach(model.updateLines.indices, id: \.self) { index in
                    Text(model.updateLines[index])
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
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
}
