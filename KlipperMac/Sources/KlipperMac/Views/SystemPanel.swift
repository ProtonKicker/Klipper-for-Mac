import SwiftUI

struct SystemPanel: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Panel("System", icon: "desktopcomputer", tint: .indigo, expands: true, plain: true) {
            Button {
                Task { await model.refreshSystem() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .help("Reload system info")
        } content: {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    group("Host") {
                        row("Hostname", model.systemInfo.hostname)
                        row("OS", model.systemInfo.os)
                        row("Kernel", model.systemInfo.kernelVersion)
                    }
                    group("Processor") {
                        row("CPU", model.systemInfo.cpuInfo)
                        row("Cores", model.systemInfo.cpuCount > 0 ? "\(model.systemInfo.cpuCount)" : "—")
                        row("Memory", model.systemInfo.memoryTotal > 0
                            ? Fmt.fileSize(model.systemInfo.memoryTotal) : "—")
                    }
                    group("Versions") {
                        row("Moonraker", model.moonrakerVersion.isEmpty ? "—" : model.moonrakerVersion)
                        row("Klipper", model.klipperVersion.isEmpty ? "—" : model.klipperVersion)
                        row("Pinned Klipper", short(model.env.pins["klipper"] ?? ""))
                    }
                    Divider()
                    HStack(spacing: 8) {
                        Button("Restart Klipper") { Task { await model.restartKlipper() } }
                        Button("Firmware Restart") { model.runGcode("FIRMWARE_RESTART") }
                            .disabled(!model.live.isReady)
                        Spacer()
                    }
                }
                .padding(4)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func group(_ title: String, @ViewBuilder _ content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            content()
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(value).textSelection(.enabled)
            Spacer()
        }
        .font(.callout)
    }

    private func short(_ s: String) -> String {
        s.count > 10 ? String(s.prefix(7)) + "…" : s
    }
}
