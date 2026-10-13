import SwiftUI

// Component versions and diagnostics.

struct ComponentsBlock: View {
    @ObservedObject var model: AppModel
    private var env: KEnvironment { model.env }

    private static let components = ["klipper", "moonraker", "mainsail", "fluidd"]

    var body: some View {
        PanelSection("Components", trailing: {
            HStack(spacing: 8) {
                if model.busy && !model.showSheet {
                    ProgressView().controlSize(.small)
                }
                Button("Check") {
                    Task { await model.checkUpdates() }
                }
                .buttonStyle(DemoButton(small: true))
                .disabled(model.busy)

                Button("Apply…") {
                    Task { await model.applyUpdates() }
                }
                .buttonStyle(DemoButton(small: true))
                .disabled(model.busy || model.snapshot.anyUp)
                .help("Stop the stack first")
            }
        }, content: {
            VStack(spacing: 0) {
                ForEach(Array(Self.components.enumerated()), id: \.element) { idx, comp in
                    if idx > 0 { Hairline() }
                    PanelListRow(name: {
                        Text(comp)
                            .font(.panelMono(12.5, weight: .medium))
                            .foregroundStyle(Palette.textPrimary)
                    }, detail: {
                        Text(pinText(env.pins, comp))
                            .textSelection(.enabled)
                    })
                }
            }

            if !model.updateLines.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(model.updateLines.enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(.panelMono(11.5))
                            .foregroundStyle(Palette.textSecondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        })
    }

    private func pinText(_ pins: [String: String], _ comp: String) -> String {
        let pin = pins[comp] ?? "?"
        let isSHA = pin.count == 40 && pin.allSatisfy { $0.isHexDigit }
        if let sha = pins[comp + "_sha"], !sha.isEmpty {
            return (isSHA ? String(sha.prefix(7)) : pin) + "  \u{00B7}  " + String(sha.prefix(7))
        }
        return isSHA ? String(pin.prefix(7)) + " (commit)" : pin
    }
}

struct LogsBlock: View {
    @ObservedObject var model: AppModel
    @State private var logService = "klipper"
    @State private var follow = false
    @State private var logsOpen = false

    private var env: KEnvironment { model.env }

    private var logURL: URL {
        logService == "klipper" ? env.klippyLog : env.moonrakerLog
    }

    var body: some View {
        PanelSection("Diagnostics") {
            // Collapsed by default: the log viewer is the one heavy block on this
            // page, and building it is most of the cost of opening the window.
            Button {
                logsOpen.toggle()
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: "doc.text.magnifyingglass")
                    Text("Log viewer")
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .rotationEffect(.degrees(logsOpen ? 180 : 0))
                }
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Palette.textPrimary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .animation(.easeOut(duration: 0.15), value: logsOpen)

            if logsOpen {
                DemoSegmented(
                    items: [.init(value: "klipper", title: "Klipper (klippy.log)"),
                            .init(value: "moonraker", title: "Moonraker (moonraker.log)")],
                    selected: logService
                ) { logService = $0 }

                LogView(url: logURL, follow: follow)

                HStack(spacing: 8) {
                    HStack(spacing: 10) {
                        Text("Follow new lines")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(Palette.textSecondary)
                        DemoSwitch(isOn: follow) { follow.toggle() }
                    }
                    Spacer()
                    Button("Reveal in Finder") { model.reveal(logURL) }
                        .buttonStyle(DemoButton(small: true))
                    Button("Copy path") {
                        model.copyToPasteboard(logURL.path)
                        model.note("Log path copied")
                    }
                    .buttonStyle(DemoButton(small: true))
                }
            }
        }
    }
}
