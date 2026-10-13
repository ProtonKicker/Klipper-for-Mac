import SwiftUI

// MARK: - stack + data folder

struct StackCard: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GlassCard {
            MicroLabel("Stack")
            VStack(spacing: 0) {
                if model.snapshot.tracked {
                    ForEach(Array(model.snapshot.services.enumerated()), id: \.element.name) { idx, s in
                        if idx > 0 { Hairline() }
                        ServiceRow(name: StackReader.pretty(s.name), up: s.up)
                            .frame(height: 40)
                    }
                } else {
                    Text("Never started on this boot.")
                        .font(.system(size: 12))
                        .foregroundStyle(Skin.inkMuted)
                }
            }
            Hairline()
            VStack(alignment: .leading, spacing: 8) {
                MicroLabel("Data folder")
                HStack(spacing: 8) {
                    Text(model.env.data.path)
                        .font(.mono(11))
                        .foregroundStyle(Skin.inkSecondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                    Spacer(minLength: 8)
                    Button("Reveal") { model.reveal(model.env.data) }
                        .buttonStyle(AppleButton(small: true))
                    Button("Move…") { chooseFolder() }
                        .buttonStyle(AppleButton(small: true))
                        .disabled(model.busy)
                }
            }
            .padding(.top, 4)
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

/// One service line. A leaf: takes plain values, so the 2s poll only
/// re-renders rows whose state actually changed.
private struct ServiceRow: View {
    let name: String
    let up: Bool
    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(up ? Skin.ok : Skin.inkMuted.opacity(0.5))
                .frame(width: 6, height: 6)
            Text(name)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Skin.ink)
            Spacer(minLength: 12)
            Text(up ? "up" : "down")
                .font(.system(size: 11.5))
                .foregroundStyle(up ? Skin.inkSecondary : Skin.inkMuted)
        }
        .padding(.horizontal, 2)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - component updater

struct UpdatesCard: View {
    @ObservedObject var model: AppModel

    private let components = ["klipper", "moonraker", "mainsail", "fluidd"]

    var body: some View {
        GlassCard {
            HStack(spacing: 10) {
                MicroLabel("Components")
                Spacer()
                Button("Check") { Task { await model.checkUpdates() } }
                    .buttonStyle(AppleButton(small: true))
                    .disabled(model.busy)
                Button("Apply") { Task { await model.applyUpdates() } }
                    .buttonStyle(AppleButton(variant: .filled, small: true))
                    .disabled(model.busy || !model.env.installed || model.snapshot.anyUp)
                    .help(model.snapshot.anyUp ? "Stop the stack first" : "Apply newest pinned tags")
            }
            if model.updateLines.isEmpty {
                VStack(spacing: 0) {
                    ForEach(components, id: \.self) { name in
                        HStack {
                            Text(name)
                                .font(.system(size: 13, weight: .medium))
                                .foregroundStyle(Skin.ink)
                            Spacer(minLength: 12)
                            Text(model.env.pins[name] ?? "—")
                                .font(.mono(11.5))
                                .foregroundStyle(Skin.inkSecondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        .padding(.horizontal, 2)
                        .frame(height: 34)
                        if name != components.last { Hairline() }
                    }
                }
                .frame(maxWidth: .infinity)
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
                .background(Skin.well, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Skin.hairlineSoft, lineWidth: 1))
            }
            Text("Pinned tags this build ships with.")
                .font(.system(size: 10.5))
                .foregroundStyle(Skin.inkMuted)
        }
    }
}

// MARK: - web interface switch

struct WebUICard: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GlassCard {
            MicroLabel("Web interface")
            MiniSegmented(items: [("fluidd", "Fluidd"), ("mainsail", "Mainsail")],
                          selected: model.env.uiName) { name in
                guard name != model.env.uiName else { return }
                Task { await model.switchUI(name) }
            }
            HStack(spacing: 10) {
                Button {
                    model.openURL(model.env.webURL)
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.up.right.square")
                            .font(.system(size: 10))
                        Text("Open")
                    }
                }
                .buttonStyle(AppleButton(small: true))
                .disabled(!model.snapshot.anyUp)
                Text(model.env.webURL)
                    .font(.mono(11))
                    .foregroundStyle(Skin.inkMuted)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
        }
    }
}

// MARK: - LAN toggle

struct NetworkCard: View {
    @ObservedObject var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let on = model.env.lanEnabled
        GlassCard {
            MicroLabel("Network")
            HStack(spacing: 10) {
                Text(on ? "Local network" : "This Mac only")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Skin.ink)
                Spacer(minLength: 4)
                MiniSwitch(isOn: on, reduceMotion: reduceMotion) {
                    Task { await model.setLAN(!on) }
                }
                .disabled(model.busy)
            }
            Text(on ? "Machines on your LAN can reach the printer at \(Net.lanIP() ?? "this Mac's address")"
                    : "Only this Mac can reach the stack.")
                .font(.system(size: 11.5))
                .foregroundStyle(Skin.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("Applies after the next stack restart.")
                .font(.system(size: 10.5))
                .foregroundStyle(Skin.inkMuted)
        }
    }
}

// MARK: - preset selector

struct PresetsCard: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GlassCard {
            MicroLabel("Printer preset")
            if model.presetNames.isEmpty {
                Text("No presets found in Settings/presets.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(Skin.inkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(spacing: 0) {
                    ForEach(model.presetNames, id: \.self) { name in
                        Button {
                            Task { await model.usePreset(name) }
                        } label: {
                            HStack(spacing: 8) {
                                Text(name)
                                    .font(.system(size: 13, weight: .medium))
                                    .foregroundStyle(Skin.ink)
                                Spacer(minLength: 8)
                                Image(systemName: "arrow.right")
                                    .font(.system(size: 10, weight: .medium))
                                    .foregroundStyle(Skin.inkMuted)
                            }
                            .padding(.horizontal, 2)
                            .frame(maxWidth: .infinity)
                            .frame(height: 40)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        if name != model.presetNames.last { Hairline() }
                    }
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 2)
                Text("Switching merges the preset into printer.cfg.")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Skin.inkMuted)
            }
        }
    }
}

// MARK: - device (serial port) selector

struct DeviceCard: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GlassCard {
            MicroLabel("Device")
            HStack(spacing: 8) {
                Image(systemName: "cable.connector")
                    .font(.system(size: 12))
                    .foregroundStyle(Skin.inkSecondary)
                Text(model.currentSerial ?? "Auto-detect")
                    .font(.mono(12))
                    .foregroundStyle(Skin.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 8)
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
                }
            }
            if model.serialPorts.isEmpty {
                Text("No USB printer board detected.")
                    .font(.system(size: 11))
                    .foregroundStyle(Skin.inkMuted)
            } else {
                Text("\(model.serialPorts.count) board\(model.serialPorts.count == 1 ? "" : "s") on USB · changes apply after a restart.")
                    .font(.system(size: 11))
                    .foregroundStyle(Skin.inkMuted)
            }
        }
    }
}

// MARK: - small controls

struct MiniSegmented: View {
    let items: [(String, String)]
    let selected: String
    let select: (String) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(items, id: \.0) { item in
                Button {
                    if item.0 != selected { select(item.0) }
                } label: {
                    Text(item.1)
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(item.0 == selected ? Skin.ink : Skin.inkMuted)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5)
                        .background {
                            if item.0 == selected {
                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    .fill(Skin.card)
                                    .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous)
                                        .strokeBorder(Skin.hairline, lineWidth: 1))
                                    .shadow(color: Skin.shadowSoft, radius: 1, y: 1)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Skin.well))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
            .strokeBorder(Skin.hairlineSoft, lineWidth: 1))
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
                .frame(width: 38, height: 22)
            Circle()
                .fill(Color.white)
                .frame(width: 18, height: 18)
                .shadow(color: Skin.shadowSoft, radius: 1, y: 1)
                .offset(x: isOn ? 19 : 2)
        }
        .frame(width: 38, height: 22, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture { toggle() }
        .animation(reduceMotion ? nil : .spring(response: 0.25, dampingFraction: 0.8),
                   value: isOn)
        .accessibilityElement()
        .accessibilityLabel(isOn ? "On" : "Off")
        .accessibilityAddTraits(.isButton)
    }
}
