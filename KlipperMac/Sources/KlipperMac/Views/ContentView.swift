import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showKillConfirm = false

    var body: some View {
        GeometryReader { geo in
            ScrollView {
                PageBody(width: geo.size.width,
                         model: model,
                         lanIP: lanIP,
                         onRequestKill: { showKillConfirm = true })
                    .padding(.top, 8)
                    .padding(.bottom, 12)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .frame(minWidth: 720, minHeight: 500)
        .background(Color(nsColor: .windowBackgroundColor))
        .navigationTitle("Klipper for Mac")
        .navigationSubtitle(subtitle)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await model.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
                .keyboardShortcut("r", modifiers: .command)
            }
        }
        // An overlay pinned under the Stack column: it reserves no space, and staying
        // in the left column keeps a toast from covering the rest of the page.
        .overlay(alignment: .bottomLeading) {
            MessageBanner(notifier: model.notifier)
        }
        .sheet(isPresented: $model.showSheet) {
            SetupSheet(title: model.sheetTitle, lines: model.sheetLines,
                       running: model.sheetRunning)
        }
        .confirmationDialog("Force kill everything, including strays a normal stop would miss?",
                            isPresented: $showKillConfirm, titleVisibility: .visible) {
            Button("Kill everything", role: .destructive) {
                Task { await model.killAll() }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    /// Opens a socket, so it is read once per render instead of once per URL row.
    private var lanIP: String? {
        model.env.lanEnabled ? Net.lanIP() : nil
    }

    private var subtitle: String {
        let state: String
        switch model.snapshot.overall {
        case .running: state = "Stack running"
        case .notResponding: state = "Stack not responding"
        case .stopped: state = "Stack stopped"
        }
        if model.busy { return "\(state) · working…" }
        if let p = model.printer,
           let st = p.printState, st == "printing" || st == "paused" {
            var tail = st.capitalized
            if let prog = p.progress { tail += " · \(Int((prog * 100).rounded()))%" }
            return "\(state) · \(tail)"
        }
        return state
    }
}
