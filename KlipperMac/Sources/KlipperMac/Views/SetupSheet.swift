import SwiftUI

/// Streaming progress sheet for `setup` / `update --apply`.
struct SetupSheet: View {
    let title: String
    let lines: [String]
    let running: Bool
    let onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                if running { ActivityIndicator() }
            }
            LogTextView(text: lines.joined(separator: "\n"), follow: true)
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            HStack {
                Spacer()
                Button(running ? "Running…" : "Done", action: onClose)
                    .disabled(running)
            }
        }
        .padding(16)
        .frame(minWidth: 600, minHeight: 420)
    }
}