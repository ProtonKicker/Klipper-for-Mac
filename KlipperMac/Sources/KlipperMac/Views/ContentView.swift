import SwiftUI

enum Pane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case settings = "Settings"

    var id: String { rawValue }
    var icon: FlatIconKind {
        switch self {
        case .dashboard: return .grid
        case .settings: return .gear
        }
    }
    var tint: Color {
        switch self {
        case .dashboard: return FlatPalette.brown
        case .settings: return FlatPalette.red
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("didOpenOnce") private var didOpenOnce = false
    @State private var pane: Pane? = .dashboard

    var body: some View {
        NavigationSplitView {
            List(selection: $pane) {
                ForEach(Pane.allCases) { p in
                    Label {
                        Text(p.rawValue)
                    } icon: {
                        FlatIcon(kind: p.icon, size: 16, color: p.tint)
                    }
                    .tag(p)
                }
            }
            .navigationSplitViewColumnWidth(min: 150, ideal: 170)
            .safeAreaInset(edge: .bottom, spacing: 0) {
                MessageBanner(notifier: model.notifier)
            }
        } detail: {
            switch pane ?? .dashboard {
            case .dashboard:
                DashboardView()
            case .settings:
                SettingsView()
            }
        }
        .frame(minWidth: 820, minHeight: 560)
        .task {
            // First run only: land on Settings so the data folder, CLI and
            // printer config are where a new user starts. Later runs open on
            // the Dashboard.
            if !didOpenOnce {
                didOpenOnce = true
                pane = .settings
            }
        }
    }
}
