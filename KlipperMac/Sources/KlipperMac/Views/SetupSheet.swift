import SwiftUI

/// Streaming progress sheet for `setup` / `update --apply`.
struct SetupSheet: View {
    let title: String
    let lines: [String]
    let running: Bool
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                if running { ProgressView().controlSize(.small) }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(.system(size: 11, design: .monospaced))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .textSelection(.enabled)
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(6)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .onChange(of: lines.count) { _ in
                    proxy.scrollTo("end", anchor: .bottom)
                }
            }
            HStack {
                Spacer()
                Button(running ? "Running…" : "Done") { dismiss() }
                    .keyboardShortcut(running ? nil : .defaultAction)
                    .disabled(running)
            }
        }
        .padding(16)
        .frame(minWidth: 600, minHeight: 420)
    }
}
