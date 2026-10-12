import SwiftUI

struct MacroPanel: View {
    @EnvironmentObject private var model: AppModel

    private let columns = [GridItem(.adaptive(minimum: 110), spacing: 6)]

    var body: some View {
        Panel("Macros", icon: "scroll", tint: .teal, expands: true, plain: true) {
            if model.macros.isEmpty {
                Text("No macros in printer.cfg")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 8) {
                        ForEach(model.macros, id: \.self) { name in
                            Button(name) { model.runGcode(name) }
                                .frame(maxWidth: .infinity)
                                .controlSize(.large)
                        }
                    }
                    .padding(2)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}
