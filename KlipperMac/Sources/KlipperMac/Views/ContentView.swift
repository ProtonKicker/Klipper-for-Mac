import SwiftUI

enum Pane: String {
    case printer = "Printer"
    case dashboard = "Klipper"
}

enum NavItem: Hashable {
    case klipper
    case settings
    case printer(PrinterSection)
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("defaultTab") private var defaultTab = Pane.printer.rawValue
    @AppStorage("printerSection") private var lastSection = PrinterSection.dashboard.rawValue
    @State private var nav: NavItem?
    @State private var printerExpanded = true

    init() {
        let d = UserDefaults.standard
        if d.string(forKey: "defaultTab") == Pane.dashboard.rawValue {
            _nav = State(initialValue: .klipper)
        } else {
            let s = PrinterSection(rawValue: d.string(forKey: "printerSection") ?? "")
                ?? .dashboard
            _nav = State(initialValue: .printer(s))
        }
    }

    var body: some View {
        NavigationSplitView {
            List(selection: $nav) {
                Label("Klipper", systemImage: "gauge")
                    .tag(NavItem.klipper)
                Label("Settings", systemImage: "gearshape.fill")
                    .tag(NavItem.settings)

                DisclosureGroup(isExpanded: $printerExpanded) {
                    ForEach(PrinterSection.allCases) { s in
                        Label(s.rawValue, systemImage: s.symbol)
                            .tag(NavItem.printer(s))
                    }
                } label: {
                    Label("Printer", systemImage: "printer.fill")
                        .font(.body.weight(.medium))
                }
            }
            .navigationSplitViewColumnWidth(min: 210, ideal: 235)
            .safeAreaInset(edge: .bottom, spacing: 0) { MessageBanner() }
        } detail: {
            detail
        }
        .frame(minWidth: 960, minHeight: 620)
        .onChange(of: printerExpanded) { expanded in
            // Clicking a collapsed Printer folder opens it on the Dashboard.
            guard expanded else { return }
            if case .printer = nav {} else {
                nav = .printer(PrinterSection(rawValue: lastSection) ?? .dashboard)
            }
        }
        .onChange(of: nav) { newValue in
            if case .printer(let s) = newValue { lastSection = s.rawValue }
        }
    }

    @ViewBuilder private var detail: some View {
        switch nav {
        case .klipper:
            DashboardView()
        case .settings:
            SettingsView()
        case .printer(let section):
            PrinterView(section: section)
        case nil:
            PrinterView(section: .dashboard)
        }
    }
}
