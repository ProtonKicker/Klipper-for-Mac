import AppKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    private var env: KEnvironment { model.env }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Card { aboutSection }
                Card { componentsSection }
                Card { dataSection }
            }
            .padding(16)
            .frame(maxWidth: 640, alignment: .leading)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - about

    private var aboutSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("About")
            FieldRow(label: "App version", value: appVersion)
            HStack(spacing: 8) {
                Button(model.appUpdateButtonTitle) { Task { await model.updateApp() } }
                    .disabled(model.appUpdateBusy || (model.availableAppUpdate != nil && model.snapshot.anyUp))
                if model.availableAppUpdate != nil && !model.appUpdateBusy {
                    Button("Check again") { Task { await model.checkForAppUpdate() } }
                        .disabled(model.appUpdateBusy)
                }
                if model.appUpdateBusy { ActivityIndicator() }
                Spacer()
            }
            if model.availableAppUpdate != nil && model.snapshot.anyUp {
                Text("Stop the Klipper stack to install the update. The app will restart to finish when the stack is not running.")
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if !model.appUpdateStatus.isEmpty {
                Text(model.appUpdateStatus)
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Button("Project on GitHub") {
                model.openURL("https://github.com/ProtonKicker/Klipper-for-Mac")
            }
        }
    }

    // MARK: - components

    private var componentsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Components")
            if env.installed {
                let pins = env.pins
                FieldRow(label: "Klipper", value: SettingsPresentation.pinText(pins, component: "klipper"))
                FieldRow(label: "Moonraker", value: SettingsPresentation.pinText(pins, component: "moonraker"))
                FieldRow(label: "Mainsail", value: SettingsPresentation.pinText(pins, component: "mainsail"))
                FieldRow(label: "Fluidd", value: SettingsPresentation.pinText(pins, component: "fluidd"))
            }
            HStack(spacing: 8) {
                Button("Check") { Task { await model.checkUpdates() } }
                    .disabled(model.busy || model.componentTaskRunning)
                Button("Update") { Task { await model.applyUpdates() } }
                    .disabled(model.busy || model.componentTaskRunning || model.snapshot.anyUp || !env.installed)
                if model.componentTaskRunning { ActivityIndicator() }
                Spacer()
            }
            if model.snapshot.anyUp && env.installed {
                Text("Stop the Klipper stack before applying component updates.")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if model.componentTaskRunning || !model.componentLog.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    if model.componentTaskRunning {
                        Text(model.componentTaskTitle.isEmpty ? "Working…" : model.componentTaskTitle)
                            .font(.callout)
                            .foregroundColor(.secondary)
                    }
                    LogTextView(text: model.componentLog.joined(separator: "\n"),
                                follow: model.componentTaskRunning)
                        .frame(maxWidth: .infinity)
                        .frame(height: 200)
                        .background(compatNSColor(.textBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .overlay(
                            RoundedRectangle(cornerRadius: 6)
                                .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                        )
                    if !model.componentTaskRunning && !model.componentLog.isEmpty {
                        HStack {
                            Spacer()
                            Button("Clear") { model.componentLog = [] }
                                .buttonStyle(.plain)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
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
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Data folder")
            Text(env.data.path)
                .font(.system(size: 11, design: .monospaced))
            HStack(spacing: 8) {
                Button("Change…") { chooseDataFolder() }
                    .disabled(model.snapshot.anyUp)
                Button("Show in Finder") { model.reveal(env.data) }
                Spacer()
            }
            if model.snapshot.anyUp {
                Text("Stop the stack before moving the data folder.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title).font(.headline)
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
