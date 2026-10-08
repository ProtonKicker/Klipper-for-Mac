import SwiftUI

enum Pane: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case printer = "Printer"
    case settings = "Settings"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .dashboard: return "gauge"
        case .printer: return "printer"
        case .settings: return "gearshape.fill"
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @State private var pane: Pane? = .dashboard

    var body: some View {
        NavigationSplitView {
            List(selection: $pane) {
                ForEach(Pane.allCases) { p in
                    Label(p.rawValue, systemImage: p.symbol).tag(p)
                }
            }
            .navigationSplitViewColumnWidth(min: 190, ideal: 210)
            .safeAreaInset(edge: .bottom, spacing: 0) { MessageBanner() }
        } detail: {
            switch pane ?? .dashboard {
            case .dashboard:
                DashboardView()
            case .printer:
                PrinterView()
            case .settings:
                SettingsView()
            }
        }
        .frame(minWidth: 820, minHeight: 560)
    }
}
