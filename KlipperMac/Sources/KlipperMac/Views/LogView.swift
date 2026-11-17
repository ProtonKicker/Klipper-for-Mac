import SwiftUI

struct LogView: View {
    let url: URL
    var follow: Bool
    @State private var text = ""
    @State private var reading = false

    var body: some View {
        LogTextView(text: text, follow: follow)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(Color.secondary.opacity(0.25), lineWidth: 1)
            )
            .frame(height: 220)
            .onAppear { reload() }
            .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { _ in
                if follow { reload() }
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
                // Old synchronous APIs: the throwing seekToEnd()/readToEnd()
                // variants need macOS 10.15.4, we floor at 10.15.0.
                let size: UInt64 = h.seekToEndOfFile()
                let window: UInt64 = 65_536
                let start = size > window ? size - window : 0
                try? h.seek(toOffset: start)
                let data = h.readDataToEndOfFile()
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