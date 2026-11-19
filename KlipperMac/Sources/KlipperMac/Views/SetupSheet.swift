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
            LogTextView(text: lines.joined(separator: "\n"), follow: running)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(compatNSColor(.textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                )
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