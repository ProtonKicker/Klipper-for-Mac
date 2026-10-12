import SwiftUI

struct PrintControlPanel: View {
    @EnvironmentObject private var model: AppModel

    private var live: LivePrinterState { model.live }

    var body: some View {
        Panel("Print", icon: "printer.fill", tint: .blue) {
            if live.printState == "printing" || live.printState == "paused" {
                VStack(alignment: .leading, spacing: 6) {
                    Text(live.filename ?? "unknown file")
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                    ProgressView(value: min(max(live.progress ?? 0, 0), 1))
                    HStack {
                        Text("\(Int(((live.progress ?? 0) * 100).rounded()))%")
                        Spacer()
                        if let d = live.printDuration {
                            Text("elapsed \(Fmt.duration(d))")
                        }
                        if let eta = remaining, eta > 0 {
                            Text("left \(Fmt.duration(eta))")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)

                    HStack(spacing: 8) {
                        if live.printState == "printing" {
                            Button("Pause") { Task { await model.pausePrint() } }
                        }
                        if live.printState == "paused" {
                            Button("Resume") { Task { await model.resumePrint() } }
                        }
                        Button("Cancel", role: .destructive) { Task { await model.cancelPrint() } }
                    }
                }
            } else {
                HStack {
                    Text(stateText)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }
            Divider()
            Button(role: .destructive) {
                model.emergencyStop()
            } label: {
                Label("Emergency Stop", systemImage: "stop.circle.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
        }
    }

    private var stateText: String {
        switch live.printState {
        case "standby": return "Standby — ready to print"
        case "complete": return "Last print complete"
        case "cancelled": return "Print cancelled"
        case .none: return "Waiting for print state…"
        default: return live.printState?.capitalized ?? ""
        }
    }

    private var remaining: Double? {
        guard let p = live.progress, let d = live.printDuration, p > 0.02 else { return nil }
        return d / p - d
    }
}
