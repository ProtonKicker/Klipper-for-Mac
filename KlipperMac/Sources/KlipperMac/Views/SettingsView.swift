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
            Button {
                model.openURL("https://github.com/ProtonKicker/Klipper-for-Mac")
            } label: {
                Text("Project on GitHub")
            }
        }
    }

    // MARK: - components

    private var componentsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("Components")
            if env.installed {
                let pins = env.pins
                FieldRow(label: "Klipper", value: pinText(pins, "klipper"))
                FieldRow(label: "Moonraker", value: pinText(pins, "moonraker"))
                FieldRow(label: "Mainsail", value: pinText(pins, "mainsail"))
                FieldRow(label: "Fluidd", value: pinText(pins, "fluidd"))
            } else {
                Text("Not installed yet — run Setup first.")
                    .font(.callout)
                    .foregroundColor(.secondary)
            }
            HStack(spacing: 8) {
                Button("Check") { Task { await model.checkUpdates() } }
                    .disabled(model.busy)
                Button("Update") { Task { await model.applyUpdates() } }
                    .disabled(model.busy || model.snapshot.anyUp || !env.installed)
                if model.busy && !model.showSheet {
                    ActivityIndicator()
                }
                Spacer()
            }
            if !model.updateLines.isEmpty {
                ForEach(Array(model.updateLines.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(.callout)
                        .foregroundColor(.secondary)
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