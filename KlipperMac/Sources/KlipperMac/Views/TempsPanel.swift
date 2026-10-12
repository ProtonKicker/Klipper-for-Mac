import SwiftUI

struct TempsPanel: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Panel("Temperatures", icon: "thermometer.medium", tint: .orange) {
            HStack(alignment: .top, spacing: 24) {
                heaterBlock("Hotend", model.live.hotend, symbol: "flame")
                heaterBlock("Bed", model.live.bed, symbol: "square.3.layers.3d")
                Spacer()
            }
            Divider()
            HStack(spacing: 6) {
                Button("PLA") { model.preheat(extruder: 210, bed: 60) }
                Button("PETG") { model.preheat(extruder: 235, bed: 80) }
                Button("ABS") { model.preheat(extruder: 255, bed: 100) }
                Spacer()
                Button("Cool down") { model.coolDown() }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        }
    }

    private func heaterBlock(_ name: String, _ h: Heater, symbol: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(name, systemImage: symbol)
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(String(format: "%.0f", h.temp))
                    .font(.system(size: 28, weight: .semibold, design: .rounded))
                    .contentTransition(.numericText())
                Text("°C").font(.system(size: 15, design: .rounded))
                    .foregroundStyle(.secondary)
                if h.target > 0 {
                    Text("/ \(Int(h.target.rounded()))°")
                        .font(.system(size: 15, design: .rounded))
                        .foregroundStyle(.orange)
                }
            }
            ProgressView(value: min(max(h.power ?? 0, 0), 1))
                .progressViewStyle(.linear)
                .tint((h.power ?? 0) > 0.02 ? .orange : .gray.opacity(0.25))
                .frame(width: 110)
        }
    }
}
