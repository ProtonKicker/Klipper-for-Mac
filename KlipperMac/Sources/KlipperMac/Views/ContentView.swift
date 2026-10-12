import SwiftUI

enum Pane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case settings = "Settings"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .dashboard: return "gauge"
        case .settings: return "gearshape.fill"
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
                    Label(p.rawValue, systemImage: p.symbol).tag(p)
                }
            }
            .navigationSplitViewColumnWidth(min: 190, ideal: 210)
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
