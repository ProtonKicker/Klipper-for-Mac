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
    @State private var pane: Pane = .printer
    @State private var didOpenOnce = UserDefaults.standard.bool(forKey: "didOpenOnce")

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                VStack(spacing: 2) {
                    ForEach(Pane.allCases) { p in
                        Button(action: { pane = p }) {
                            HStack(spacing: 8) {
                                FlatIcon(kind: p.icon, size: 16, color: pane == p ? .primary : p.tint)
                                Text(p.rawValue)
                                    .foregroundColor(.primary)
                                Spacer()
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 7)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                RoundedRectangle(cornerRadius: 6)
                                    .fill(pane == p ? Color.secondary.opacity(0.18) : Color.clear)
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(8)
                Spacer(minLength: 0)
                MessageBanner(notifier: model.notifier)
            }
            .frame(width: 170)
            .background(compatNSColor(.windowBackgroundColor))

            Divider()

            Group {
                switch pane {
                case .printer:
                    DashboardView()
                case .camera:
                    CameraView()
                case .settings:
                    SettingsView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
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
}
