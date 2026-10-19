import SwiftUI
import AppKit

struct KeyCaptureView: NSViewRepresentable {
    let onKey: (String) -> Void

    func makeNSView(context: Context) -> KeyCaptureNSView {
        let view = KeyCaptureNSView()
        view.onKey = onKey
        return view
    }

    func updateNSView(_ nsView: KeyCaptureNSView, context: Context) {
        nsView.onKey = onKey
    }
}

final class KeyCaptureNSView: NSView {
    var onKey: ((String) -> Void)?
    private var monitor: Any?

    override var acceptsFirstResponder: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window != nil && monitor == nil {
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let chars = event.charactersIgnoringModifiers,
                      chars.count == 1, let self else { return event }
                switch chars {
                case "1", "2", "3": self.onKey?(chars); return nil
                default: return event
                }
            }
        }
    }

    deinit {
        if let monitor {
            NSEvent.removeMonitor(monitor)
        }
    }
}

enum Pane: String, CaseIterable, Identifiable {
    case printer = "Printer"
    case camera = "Recording"
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
        .overlay(
            KeyCaptureView { key in
                switch key {
                case "1": pane = .printer
                case "2": pane = .camera
                case "3": pane = .settings
                default: break
                }
            }
            .frame(width: 0, height: 0)
            .opacity(0)
            .allowsHitTesting(false)
        )
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
