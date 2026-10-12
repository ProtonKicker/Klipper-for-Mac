import SwiftUI

struct HistoryPanel: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Panel("History", icon: "clock.arrow.circlepath", tint: .green, expands: true, plain: true) {
            Button {
                Task { await model.refreshHistory() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .help("Reload history")
        } content: {
            let t = model.historyTotals
            HStack(spacing: 0) {
                stat("Jobs", "\(t.jobs)")
                Divider().frame(height: 40)
                stat("Print time", Fmt.duration(t.totalPrintTime))
                Divider().frame(height: 40)
                stat("Longest", Fmt.duration(t.longestPrint))
                Divider().frame(height: 40)
                stat("Filament", String(format: "%.0f m", t.filamentUsed / 1000))
            }
            .padding(.vertical, 6)
            Divider()
            if model.historyJobs.isEmpty {
                Text("No completed jobs yet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.top, 8)
            } else {
                ScrollView {
                    LazyVStack(spacing: 2) {
                        ForEach(model.historyJobs) { job in
                            HStack(spacing: 10) {
                                Circle().fill(statusColor(job.status)).frame(width: 7, height: 7)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(job.filename).lineLimit(1)
                                    Text(Fmt.date(job.endDate))
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                Spacer()
                                VStack(alignment: .trailing, spacing: 1) {
                                    Text(Fmt.duration(job.printDuration))
                                        .font(.system(.body, design: .monospaced))
                                    Text(String(format: "%.0f m", job.filamentUsed / 1000))
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                            .padding(.vertical, 4).padding(.horizontal, 4)
                            Divider()
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(spacing: 3) {
            Text(value).font(.system(.title3, design: .rounded).weight(.semibold))
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    private func statusColor(_ s: String) -> Color {
        switch s {
        case "Completed": return .green
        case "Cancelled": return .orange
        case "Error": return .red
        default: return .secondary
        }
    }
}
