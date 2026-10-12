import SwiftUI

/// Moonraker-served config editor — works even while Klipper is not connected.
struct ConfigPanel: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Panel("Config", icon: "slider.horizontal.3", tint: .indigo, expands: true, plain: true) {
            Button {
                Task { await model.loadConfigList() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .help("Reload config files")
        } content: {
            HStack {
                Picker("File", selection: Binding(
                    get: { model.configName },
                    set: { name in
                        if name != model.configName {
                            Task { await model.loadConfig(name) }
                        }
                    }
                )) {
                    ForEach(model.configFiles, id: \.self) { Text($0).tag($0) }
                }
                .frame(maxWidth: 220)
                Spacer()
                if model.configDirty {
                    Text("unsaved changes")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }

            TextEditor(text: Binding(
                get: { model.configText },
                set: { model.configText = $0; model.configDirty = true }
            ))
            .font(.system(.caption, design: .monospaced))
            .frame(maxWidth: .infinity)
            .frame(minHeight: 240, idealHeight: 420, maxHeight: 640)
            .padding(8)
            .background(Color(nsColor: .textBackgroundColor))
            .cornerRadius(6)
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.separator))

            HStack(spacing: 8) {
                Button("Save") {
                    Task { await model.saveConfig() }
                }
                .disabled(!model.configDirty)
                Button("Restart Klipper") {
                    Task { await model.restartKlipper() }
                }
                .help("Apply config changes: klippy restarts and reloads printer.cfg")
                Spacer()
            }
        }
    }
}
