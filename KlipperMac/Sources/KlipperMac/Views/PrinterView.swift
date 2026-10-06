import SwiftUI

struct PrinterView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Section("Controls") {
                HStack(spacing: 12) {
                    StatusBadge(overall: model.snapshot.overall)
                    if model.busy { ProgressView().controlSize(.small) }
                    Spacer()
                }
                HStack(spacing: 8) {
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
                    Spacer()
                }
            }

            if let p = model.printer {
                printerSection(p)
            } else {
                Section("Printer") {
                    Text(model.snapshot.anyUp
                         ? "Waiting for Moonraker to answer on \(model.env.moonrakerURL)…"
                         : "Start the stack to see temperatures, print progress and pause/resume here.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Printer")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await model.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                        .help("Refresh")
            }
        }
    }

    @ViewBuilder private func printerSection(_ p: PrinterStatus) -> some View {
        Section("Printer") {
            LabeledContent("Klipper",
                           value: p.connected ? "connected (\(p.klippyState))"
                                              : "not connected (\(p.klippyState))")
            if let st = p.printState {
                LabeledContent("State", value: st.capitalized)
            }
            if let file = p.filename, !file.isEmpty {
                LabeledContent("File", value: file)
                if let prog = p.progress {
                    VStack(alignment: .leading, spacing: 4) {
                        ProgressView(value: min(max(prog, 0), 1))
                        Text("\(Int((prog * 100).rounded()))%")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            if let h = p.hotend {
                LabeledContent("Hotend",
                               value: String(format: "%.0f / %.0f °C", h.temp, h.target))
            }
            if let b = p.bed {
                LabeledContent("Bed",
                               value: String(format: "%.0f / %.0f °C", b.temp, b.target))
            }
            if let pos = p.position {
                LabeledContent("Position",
                               value: String(format: "X %.1f   Y %.1f   Z %.2f",
                                             pos.x, pos.y, pos.z))
            }
            if let m = p.message, !m.isEmpty {
                Text(m)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if p.printState == "printing" || p.printState == "paused" {
                HStack {
                    if p.printState == "printing" {
                        Button("Pause") { Task { await model.pausePrint() } }
                    }
                    if p.printState == "paused" {
                        Button("Resume") { Task { await model.resumePrint() } }
                    }
                    Button("Cancel print", role: .destructive) {
                        Task { await model.cancelPrint() }
                    }
                }
            }
        }
    }
}
