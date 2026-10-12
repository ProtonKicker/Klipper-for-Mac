import SwiftUI

struct ConsolePanel: View {
    @EnvironmentObject private var model: AppModel
    @State private var input = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        Panel("Console", icon: "terminal.fill", tint: .gray, expands: true, plain: true) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(model.console) { line in
                            Text(line.text)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(color(for: line.kind))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(line.id)
                        }
                    }
                    .padding(.vertical, 4)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .onChange(of: model.console.count) { _ in
                    if let last = model.console.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
            HStack {
                TextField("G-code command…", text: $input)
                    .textFieldStyle(.roundedBorder)
                    .focused($inputFocused)
                    .onSubmit(send)
                Button("Send", action: send)
                    .keyboardShortcut(.defaultAction)
                    .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
    }

    private func send() {
        let cmd = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cmd.isEmpty else { return }
        model.runGcode(cmd)
        input = ""
        inputFocused = true
    }

    private func color(for kind: ConsoleLine.Kind) -> Color {
        switch kind {
        case .sent: return .primary
        case .response: return .secondary
        case .error: return .red
        case .system: return .blue
        }
    }
}
