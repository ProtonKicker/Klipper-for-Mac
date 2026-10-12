import SwiftUI

/// Mainsail-style pages within the Printer section, driven by the sidebar.
enum PrinterSection: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case macros = "Macros"
    case files = "Files"
    case heightmap = "Heightmap"
    case console = "Console"
    case config = "Config"
    case history = "History"
    case system = "System"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .dashboard: return "square.grid.2x2"
        case .macros: return "scroll"
        case .files: return "doc.text"
        case .heightmap: return "cube"
        case .console: return "terminal"
        case .config: return "slider.horizontal.3"
        case .history: return "clock.arrow.circlepath"
        case .system: return "desktopcomputer"
        }
    }
}

struct PrinterView: View {
    @EnvironmentObject private var model: AppModel
    let section: PrinterSection

    private let columns = [GridItem(.adaptive(minimum: 380, maximum: 600),
                                    spacing: 20, alignment: .top)]

    var body: some View {
        Group {
            if !model.snapshot.anyUp {
                stoppedCard
            } else {
                content
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
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

    // MARK: - per-section content

    @ViewBuilder private var content: some View {
        switch section {
        case .dashboard:
            VStack(spacing: 0) {
                LiveHeader()
                Divider()
                if model.live.isReady {
                    ScrollView {
                        LazyVGrid(columns: columns, alignment: .leading, spacing: 20) {
                            PrintControlPanel()
                            MovePanel()
                            TempsPanel()
                        }
                        .padding(24)
                        .frame(maxWidth: 1320)
                    }
                } else {
                    VStack(spacing: 14) {
                        notReadyBanner
                        VStack(spacing: 8) {
                            Image(systemName: "cable.connector")
                                .font(.system(size: 30))
                                .foregroundStyle(.secondary)
                            Text("Live controls appear once Klipper is connected.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Text("Files, Console, Config, History and System still work — pick them in the sidebar.")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                                .multilineTextAlignment(.center)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    .padding(24)
                }
            }
        case .macros:
            page(MacroPanel())
        case .files:
            page(FileListPanel())
        case .heightmap:
            page(HeightmapPanel())
        case .console:
            page(ConsolePanel())
        case .config:
            page(ConfigPanel())
        case .history:
            page(HistoryPanel())
        case .system:
            page(SystemPanel())
        }
    }

    /// Full-page panels fill the detail pane edge-to-edge (plain style).
    private func page<V: View>(_ v: V) -> some View {
        v.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var stoppedCard: some View {
        VStack(spacing: 8) {
            Image(systemName: "pause.circle")
                .font(.system(size: 34))
                .foregroundStyle(.secondary)
            Text("Start the stack (Klipper tab) to see live printer status here.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var faulty: Bool {
        model.live.klippy == .shutdown || model.live.klippy == .error
    }

    private var notReadyBanner: some View {
        HStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title3)
                .foregroundStyle(faulty ? Color.red : .orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(faulty
                     ? "Klipper reported \(model.live.klippy.rawValue.uppercased())."
                     : "Klipper is not connected yet (\(model.live.klippy.rawValue)).")
                    .font(.callout.weight(.semibold))
                Text("Check the USB serial device on the Klipper tab, then restart the stack.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Emergency Stop", role: .destructive) { model.emergencyStop() }
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill((faulty ? Color.red : Color.orange).opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder((faulty ? Color.red : Color.orange).opacity(0.35),
                              lineWidth: 0.5)
        )
    }
}
