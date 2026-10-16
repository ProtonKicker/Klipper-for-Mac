import SwiftUI

enum Pane: String, CaseIterable, Identifiable {
    case printer = "Printer"
    case camera = "Camera"
    case settings = "Settings"

    var id: String { rawValue }
    var icon: FlatIconKind {
        switch self {
        case .printer: return .grid
        case .camera: return .camera
        case .settings: return .gear
        }
    }
    var tint: Color {
        switch self {
        case .printer: return FlatPalette.brown
        case .camera: return FlatPalette.blue
        case .settings: return FlatPalette.red
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("didOpenOnce") private var didOpenOnce = false
    @State private var pane: Pane? = .printer
    @State private var columnVisibility: NavigationSplitViewVisibility = .all

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            List(selection: $pane) {
                ForEach(Pane.allCases) { p in
                    Label {
                        Text(p.rawValue)
                    } icon: {
                        FlatIcon(kind: p.icon, size: 16, color: pane == p ? .primary : p.tint)
                    }
                    .tag(p)
                }
            }
            // Fixed width: during expand the list and detail keep the same
            // layout every frame, matching the slide used on collapse.
            .navigationSplitViewColumnWidth(170)
            .safeAreaInset(edge: .bottom, spacing: 0) {
                MessageBanner(notifier: model.notifier)
            }
        } detail: {
            switch pane ?? .printer {
            case .printer:
                DashboardView()
            case .camera:
                CameraView()
            case .settings:
                SettingsView()
            }
        }
        .animation(.easeInOut(duration: 0.22), value: columnVisibility)
        .frame(minWidth: 820, minHeight: 560)
        .task {
            // First run only: land on Settings so the data folder, CLI and
            // printer config are where a new user starts. Later runs open on
            // the Printer pane.
            if !didOpenOnce {
                didOpenOnce = true
                pane = .settings
            }
        }
    }
}
