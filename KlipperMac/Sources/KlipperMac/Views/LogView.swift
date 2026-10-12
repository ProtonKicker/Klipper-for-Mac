import SwiftUI

struct LogView: View {
    let url: URL
    var follow: Bool
    @State private var text = ""
    @State private var reading = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text(text.isEmpty ? "(no log yet)" : text)
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                    Color.clear.frame(height: 1).id("end")
                }
                .padding(6)
            }
            .frame(height: 220)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(Color.secondary.opacity(0.25), lineWidth: 1)
            )
            .onAppear { reload() }
            .onChange(of: url) { _ in reload() }
            .onChange(of: text) { _ in
                if follow { proxy.scrollTo("end", anchor: .bottom) }
            }
            // A published 1s timer stays attached for as long as the view exists, so
            // it kept tailing the log every second even with the viewer collapsed and
            // while the app was in the background. This runs only while following,
            // and only while the app is frontmost.
            .task(id: follow && scenePhase == .active) {
                guard follow, scenePhase == .active else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    if Task.isCancelled { break }
                    reload()
                }
            }
        }
    }

    private func reload() {
        guard !reading else { return }
        reading = true
        let url = self.url
        DispatchQueue.global(qos: .utility).async {
            var tail = ""
            if let h = try? FileHandle(forReadingFrom: url) {
                defer { try? h.close() }
                let size = (try? h.seekToEnd()) ?? 0
                let window: UInt64 = 65_536
                let start = size > window ? size - window : 0
                try? h.seek(toOffset: start)
                let data = (try? h.readToEnd()) ?? Data()
                var s = String(data: data, encoding: .utf8) ?? String(decoding: data, as: UTF8.self)
                if start > 0, let nl = s.firstIndex(of: "\n") {
                    s = String(s[s.index(after: nl)...])
                }
                let lines = s.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
                // Layout cost of one selectable Text grows fast with line count and
                // the pane shows ~12 lines; 200 is plenty of scrollback.
                tail = lines.suffix(200).joined(separator: "\n")
            }
            DispatchQueue.main.async {
                reading = false
                if tail != text { text = tail }
            }
        }
    }
}
