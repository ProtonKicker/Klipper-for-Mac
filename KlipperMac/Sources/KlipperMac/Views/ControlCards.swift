import SwiftUI

// MARK: - stack table

struct StackTable: View {
    @ObservedObject var model: AppModel

    var body: some View {
        NotionSection(title: "Stack", symbol: "square.stack.3d.down.right") {
            if model.snapshot.tracked {
                ForEach(Array(model.snapshot.services.enumerated()), id: \.element.name) { idx, s in
                    if idx > 0 { TableDivider() }
                    ServiceRow(name: StackReader.pretty(s.name), up: s.up)
                }
            } else {
                TInfoRow(text: "Never started on this boot.")
            }
            TableDivider()
            DataFolderRow(model: model)
        }
    }
}

/// One service line. A leaf: takes plain values, so the 2s poll only
/// re-renders rows whose state actually changed.
private struct ServiceRow: View {
    let name: String
    let up: Bool
    var body: some View {
        TRow(symbol: "doc.text", title: name, propertyWidth: 130) {
            if up {
                Chip(text: "Running", bg: Skin.chipGreenBG, ink: Skin.chipGreenInk)
            } else {
                Chip(text: "Stopped", bg: Skin.chipGrayBG, ink: Skin.chipGrayInk)
            }
        }
    }
}

private struct DataFolderRow: View {
    @ObservedObject var model: AppModel

    var body: some View {
        TRow(symbol: "folder", title: model.env.data.path, propertyWidth: 130) {
            TextButton(text: "Reveal") { model.reveal(model.env.data) }
            TextButton(text: "Move…", enabled: !model.busy) { chooseFolder() }
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Move here"
        panel.message = "Choose a new location for the Klipper data folder"
        if panel.runModal() == .OK, let url = panel.url {
            Task { await model.moveData(url) }
        }
    }
}

// MARK: - component updater

struct UpdatesTable: View {
    @ObservedObject var model: AppModel

    private let components = ["klipper", "moonraker", "mainsail", "fluidd"]

    var body: some View {
        NotionSection(title: "Components", symbol: "shippingbox") {
            TextButton(text: "Check", enabled: !model.busy) {
                Task { await model.checkUpdates() }
            }
            TextButton(text: "Apply",
                       enabled: !model.busy && model.env.installed && !model.snapshot.anyUp) {
                Task { await model.applyUpdates() }
            }
            .help(model.snapshot.anyUp ? "Stop the stack first" : "Apply newest pinned tags")
        } content: {
            if model.updateLines.isEmpty {
                ForEach(Array(components.enumerated()), id: \.element) { idx, name in
                    if idx > 0 { TableDivider() }
                    TRow(symbol: "doc.text", title: name, propertyWidth: 130) {
                        let pin = model.env.pins[name] ?? "—"
                        Text(pin.split(separator: "T").first.map(String.init) ?? pin)
                            .font(.mono(11.5))
                            .foregroundStyle(Skin.inkSecondary)
                            .lineLimit(1)
                            .help(pin)
                    }
                }
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(model.updateLines.enumerated()), id: \.offset) { _, l in
                            Text(l)
                                .font(.mono(11))
                                .foregroundStyle(Skin.inkSecondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxWidth: .infinity, maxHeight: 140)
                .background(RoundedRectangle(cornerRadius: 6).fill(Skin.well))
            }
            TInfoRow(text: "Pinned tags this build ships with.")
        }
    }
}

// MARK: - preferences table (web interface + LAN + device)

struct PreferencesTable: View {
    @ObservedObject var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        NotionSection(title: "Preferences", symbol: "switch.2") {
            TableDivider()
            WebUIRow(model: model)
            TableDivider()
            NetworkRow(model: model, reduceMotion: reduceMotion)
            TableDivider()
            DeviceRow(model: model)
            TInfoRow(text: model.serialPorts.isEmpty
                     ? "No USB printer board detected · changes apply after a restart."
                     : "\(model.serialPorts.count) board\(model.serialPorts.count == 1 ? "" : "s") on USB · changes apply after a restart.")
        }
    }
}

private struct WebUIRow: View {
    @ObservedObject var model: AppModel

    var body: some View {
        TRow(symbol: "globe", title: "Web interface", propertyWidth: 210) {
            MiniSegmented(items: [("fluidd", "Fluidd"), ("mainsail", "Mainsail")],
                          selected: model.env.uiName) { name in
                guard name != model.env.uiName else { return }
                Task { await model.switchUI(name) }
            }
            TextButton(text: "Open", enabled: model.snapshot.anyUp) {
                model.openURL(model.env.webURL)
            }
        }
    }
}

private struct NetworkRow: View {
    @ObservedObject var model: AppModel
    let reduceMotion: Bool

    var body: some View {
        let on = model.env.lanEnabled
        TRow(symbol: "network",
             title: on ? "Local network — \(Net.lanIP() ?? "this Mac")" : "This Mac only",
             propertyWidth: 210) {
            Spacer(minLength: 0)
            MiniSwitch(isOn: on, reduceMotion: reduceMotion) {
                Task { await model.setLAN(!on) }
            }
            .disabled(model.busy)
        }
    }
}

private struct DeviceRow: View {
    @ObservedObject var model: AppModel

    var body: some View {
        TRow(symbol: "cable.connector",
             title: model.currentSerial ?? "Auto-detect",
             propertyWidth: 210) {
            if model.serialPorts.count > 1 {
                Menu("Change…") {
                    ForEach(model.serialPorts) { port in
                        Button {
                            Task { await model.setSerial(port.path) }
                        } label: {
                            Text("\(port.path) — \(port.label)")
                        }
                    }
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .frame(width: 80)
            } else {
                Text(model.serialPorts.count == 1 ? "only board" : "no boards")
                    .font(.system(size: 12))
                    .foregroundStyle(Skin.inkMuted)
            }
        }
    }
}

// MARK: - preset selector

struct PresetsTable: View {
    @ObservedObject var model: AppModel

    var body: some View {
        NotionSection(title: "Printer presets", symbol: "slider.horizontal.below.rectangle") {
            if model.presetNames.isEmpty {
                TInfoRow(text: "No presets found in Settings/presets.")
            } else {
                ForEach(Array(model.presetNames.enumerated()), id: \.element) { idx, name in
                    if idx > 0 { TableDivider() }
                    Button {
                        Task { await model.usePreset(name) }
                    } label: {
                        TRow(symbol: "doc.text", title: name, propertyWidth: 60) {
                            Image(systemName: "arrow.right")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(Skin.inkMuted)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                TInfoRow(text: "Switching merges the preset into printer.cfg.")
            }
        }
    }
}

// MARK: - small controls

/// Notion's tab selector: selected item sits on a light gray pill.
struct MiniSegmented: View {
    let items: [(String, String)]
    let selected: String
    let select: (String) -> Void
    @State private var hovering: String?

    var body: some View {
        HStack(spacing: 2) {
            ForEach(items, id: \.0) { item in
                Button {
                    if item.0 != selected { select(item.0) }
                } label: {
                    Text(item.1)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(item.0 == selected ? Skin.ink : Skin.inkMuted)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background {
                            if item.0 == selected {
                                RoundedRectangle(cornerRadius: 5).fill(Skin.chipGrayBG)
                            } else if hovering == item.0 {
                                RoundedRectangle(cornerRadius: 5).fill(Skin.rowHover)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .onHover { hovering = $0 ? item.0 : (hovering == item.0 ? nil : hovering) }
            }
        }
    }
}

struct MiniSwitch: View {
    let isOn: Bool
    let reduceMotion: Bool
    let toggle: () -> Void

    var body: some View {
        ZStack(alignment: .leading) {
            Capsule()
                .fill(isOn ? Skin.accent : Skin.trackFill)
                .frame(width: 34, height: 20)
            Circle()
                .fill(Color.white)
                .frame(width: 16, height: 16)
                .offset(x: isOn ? 17 : 2)
        }
        .frame(width: 34, height: 20, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture { toggle() }
        .animation(reduceMotion ? nil : .spring(response: 0.25, dampingFraction: 0.8),
                   value: isOn)
        .accessibilityElement()
        .accessibilityLabel(isOn ? "On" : "Off")
        .accessibilityAddTraits(.isButton)
    }
}
