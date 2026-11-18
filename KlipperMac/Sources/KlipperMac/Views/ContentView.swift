import SwiftUI

enum Pane: String, CaseIterable, Identifiable {
    case printer = "Printer"
    case settings = "Settings"

    var id: String { rawValue }
    var icon: FlatIconKind {
        switch self {
        case .printer: return .grid
        case .settings: return .gear
        }
    }
    var tint: Color {
        switch self {
        case .printer: return FlatPalette.brown
        case .settings: return FlatPalette.red
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @State private var pane: Pane = .printer
    // SwiftUI @AppStorage needs macOS 11+; a UserDefaults-backed @State is
    // equivalent and works on 10.15.
    @State private var didOpenOnce = UserDefaults.standard.bool(forKey: "didOpenOnce")

    var body: some View {
        HSplitView {
            sidebar
            detail(for: pane)
        }
        .frame(minWidth: 820, minHeight: 560)
        .sheet(isPresented: $model.showSheet) {
            SetupSheet(title: model.sheetTitle, lines: model.sheetLines,
                       running: model.sheetRunning,
                       onClose: { model.showSheet = false })
        }
        .onAppear {
            // First run only: land on Settings so the data folder, CLI and
            // printer config are where a new user starts. Later runs open on
            // the Printer pane.
            if !didOpenOnce {
                UserDefaults.standard.set(true, forKey: "didOpenOnce")
                didOpenOnce = true
                pane = .settings
            }
        }
    }

    private var sidebar: some View {
        VStack(spacing: 4) {
            ForEach(Pane.allCases) { p in
                Button {
                    pane = p
                } label: {
                    HStack(spacing: 8) {
                        FlatIcon(kind: p.icon, size: 16, color: p.tint)
                        Text(p.rawValue)
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .contentShape(Rectangle())
                    .background(
                        RoundedRectangle(cornerRadius: 6)
                            .fill(pane == p
                                  ? Color.accentColor.opacity(0.15)
                                  : Color.clear)
                    )
                }
                .buttonStyle(.plain)
            }
            Spacer()
            MessageBanner(notifier: model.notifier)
        }
        .padding(6)
        .frame(minWidth: 150, idealWidth: 180, maxWidth: 220)
        .frame(maxHeight: .infinity)
    }

    @ViewBuilder
    private func detail(for pane: Pane) -> some View {
        switch pane {
        case .printer:
            DashboardView()
        case .settings:
            SettingsView()
        }
    }
}