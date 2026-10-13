import SwiftUI

// What the operator actually drives: stack control and live print telemetry.

struct SetupBlock: View {
    @ObservedObject var model: AppModel

    var body: some View {
        PanelSection("Setup") {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(Tint.attention)
                Text("Klipper stack is not installed on this Mac")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Palette.textPrimary)
            }
            Text("Installs pristine Klipper, Moonraker, Mainsail and Fluidd from GitHub. Takes a few minutes and needs internet.")
                .font(.system(size: 13))
                .foregroundStyle(Palette.textSecondary)
            HStack(spacing: 8) {
                Button {
                    Task { await model.runSetup() }
                } label: {
                    ButtonLabel(title: model.busy ? "Working…" : "Install stack",
                                symbol: "square.and.arrow.down")
                }
                .buttonStyle(DemoButton(variant: .primary))
                .disabled(model.busy)

                Button("Copy install command") {
                    model.copyToPasteboard("curl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh")
                    model.note("Install command copied")
                }
                .buttonStyle(DemoButton())
            }
        }
    }
}

struct StackBlock: View {
    @ObservedObject var model: AppModel
    let onRequestKill: () -> Void

    var body: some View {
        PanelSection("Stack") {
            HStack(spacing: 8) {
                Button("Start") {
                    Task { await model.startStack() }
                }
                .buttonStyle(DemoButton(variant: .primary))
                .disabled(model.busy || model.snapshot.anyUp || !model.env.installed)

                Button("Stop") {
                    Task { await model.stopStack() }
                }
                .buttonStyle(DemoButton())
                .disabled(model.busy || !model.snapshot.tracked)

                Button("Restart") {
                    Task { await model.restartStack() }
                }
                .buttonStyle(DemoButton())
                .disabled(model.busy || !model.env.installed)

                Button("Kill all…") {
                    onRequestKill()
                }
                .buttonStyle(DemoButton(variant: .danger))
                .disabled(model.busy || !model.snapshot.tracked)

                if model.busy { ProgressView().controlSize(.small) }
                Spacer(minLength: 0)
            }

            if model.snapshot.tracked {
                VStack(spacing: 0) {
                    ForEach(Array(model.snapshot.services.enumerated()),
                            id: \.element.name) { idx, s in
                        if idx > 0 { Hairline() }
                        StackServiceRow(name: StackReader.pretty(s.name), up: s.up)
                    }
                }
                .padding(.top, 2)
            }
        }
    }
}

/// One service line. A leaf of the page: takes plain values, so the 2s poll
/// only re-renders rows whose state actually changed.
struct StackServiceRow: View {
    let name: String
    let up: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: up ? "checkmark.circle.fill" : "xmark.circle.fill")
                .font(.system(size: 11))
                .foregroundStyle(up ? Tint.ok : Tint.down)
            Text(name)
                .font(.panelMono(12.5, weight: .medium))
                .foregroundStyle(Palette.textPrimary)
            Spacer(minLength: 12)
            Text(up ? "up" : "down")
                .font(.system(size: 11.5))
                .foregroundStyle(Palette.textSecondary)
        }
        .padding(.vertical, 11)
        .accessibilityElement(children: .combine)
    }
}

struct PrinterBlock: View {
    @ObservedObject var model: AppModel
    let printer: PrinterStatus

    var body: some View {
        PanelSection("Printer") {
            KVRow("Klipper", printer.connected
                  ? "connected (\(printer.klippyState))"
                  : "not connected (\(printer.klippyState))")
            if let st = printer.printState {
                KVRow("State", st.capitalized)
            }
            if let file = printer.filename, !file.isEmpty {
                KVRow("File", file)
                if let prog = printer.progress {
                    VStack(alignment: .leading, spacing: 4) {
                        ProgressView(value: min(max(prog, 0), 1))
                        Text("\(Int((prog * 100).rounded()))%")
                            .font(.caption)
                            .foregroundStyle(Palette.textSecondary)
                    }
                }
            }
            if let h = printer.hotend {
                KVRow(key: "Hotend") { TempValue(temp: h.temp, target: h.target) }
            }
            if let b = printer.bed {
                KVRow(key: "Bed") { TempValue(temp: b.temp, target: b.target) }
            }
            if let pos = printer.position {
                KVRow("Position",
                      String(format: "X %.1f   Y %.1f   Z %.2f", pos.x, pos.y, pos.z))
            }
            if let m = printer.message, !m.isEmpty {
                Text(m)
                    .font(.system(size: 13))
                    .foregroundStyle(Palette.textSecondary)
                    .textSelection(.enabled)
            }
            if printer.printState == "printing" || printer.printState == "paused" {
                HStack(spacing: 8) {
                    if printer.printState == "printing" {
                        Button("Pause") { Task { await model.pausePrint() } }
                            .buttonStyle(DemoButton())
                    }
                    if printer.printState == "paused" {
                        Button("Resume") { Task { await model.resumePrint() } }
                            .buttonStyle(DemoButton(variant: .primary))
                    }
                    Button("Cancel print") {
                        Task { await model.cancelPrint() }
                    }
                    .buttonStyle(DemoButton(variant: .danger))
                    Spacer(minLength: 0)
                }
                .padding(.top, 2)
            }
        }
    }
}

struct TempValue: View {
    let temp: Double
    let target: Double

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "thermometer.medium")
                .symbolRenderingMode(.palette)
                .foregroundStyle(Tint.heat, .primary)
            Text(String(format: "%.0f / %.0f °C", temp, target))
                .font(.system(size: 12.5))
                .monospacedDigit()
                .foregroundStyle(Palette.textPrimary)
        }
    }
}
