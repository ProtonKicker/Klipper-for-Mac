import SwiftUI

// What the operator actually drives: stack control and live print telemetry.

struct SetupBlock: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Section {
            Label {
                Text("Klipper stack is not installed on this Mac")
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(Tint.attention)
            }
            Text("Installs pristine Klipper, Moonraker, Mainsail and Fluidd from GitHub. Takes a few minutes and needs internet.")
                .font(.callout)
                .foregroundStyle(.secondary)
            Button {
                Task { await model.runSetup() }
            } label: {
                ButtonLabel(title: model.busy ? "Working…" : "Install stack",
                            symbol: "square.and.arrow.down")
            }
            .buttonStyle(.borderedProminent)
            .disabled(model.busy)
        } header: {
            Text("Setup")
        }
    }
}

struct StackBlock: View {
    @ObservedObject var model: AppModel
    let onRequestKill: () -> Void

    var body: some View {
        Section("Stack") {
            HStack(spacing: 8) {
                StatusBadge(overall: model.snapshot.overall)
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
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

                Button(role: .destructive) {
                    onRequestKill()
                } label: {
                    Text("Kill all…").foregroundStyle(Tint.down)
                }
                .disabled(model.busy || !model.snapshot.tracked)
            }

            if model.snapshot.tracked {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)],
                          alignment: .leading, spacing: 6) {
                    ForEach(model.snapshot.services, id: \.name) { s in
                        HStack(spacing: 6) {
                            Image(systemName: s.up ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .font(.system(size: 11))
                                .foregroundStyle(s.up ? Tint.ok : Tint.down)
                            Text(StackReader.pretty(s.name))
                            Spacer(minLength: 12)
                            Text(s.up ? "up" : "down")
                                .font(.caption2)
                                .foregroundStyle(Tint.inkSecondary)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
    }
}

struct PrinterBlock: View {
    @ObservedObject var model: AppModel
    let printer: PrinterStatus

    var body: some View {
        Section("Printer") {
            LabeledContent("Klipper",
                           value: printer.connected ? "connected (\(printer.klippyState))"
                                                    : "not connected (\(printer.klippyState))")
            if let st = printer.printState {
                LabeledContent("State", value: st.capitalized)
            }
            if let file = printer.filename, !file.isEmpty {
                LabeledContent("File", value: file)
                if let prog = printer.progress {
                    VStack(alignment: .leading, spacing: 4) {
                        ProgressView(value: min(max(prog, 0), 1))
                        Text("\(Int((prog * 100).rounded()))%")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            if let h = printer.hotend {
                LabeledContent("Hotend") {
                    HStack(spacing: 6) {
                        Image(systemName: "thermometer.medium")
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(Tint.heat, .primary)
                        Text(String(format: "%.0f / %.0f °C", h.temp, h.target))
                            .monospacedDigit()
                    }
                }
            }
            if let b = printer.bed {
                LabeledContent("Bed") {
                    HStack(spacing: 6) {
                        Image(systemName: "thermometer.medium")
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(Tint.heat, .primary)
                        Text(String(format: "%.0f / %.0f °C", b.temp, b.target))
                            .monospacedDigit()
                    }
                }
            }
            if let pos = printer.position {
                LabeledContent("Position",
                               value: String(format: "X %.1f   Y %.1f   Z %.2f",
                                             pos.x, pos.y, pos.z))
            }
            if let m = printer.message, !m.isEmpty {
                Text(m)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if printer.printState == "printing" || printer.printState == "paused" {
                HStack(spacing: 8) {
                    if printer.printState == "printing" {
                        Button("Pause") { Task { await model.pausePrint() } }
                    }
                    if printer.printState == "paused" {
                        Button("Resume") { Task { await model.resumePrint() } }
                    }
                    Button(role: .destructive) {
                        Task { await model.cancelPrint() }
                    } label: {
                        Text("Cancel print").foregroundStyle(Tint.down)
                    }
                }
            }
        }
    }
}
