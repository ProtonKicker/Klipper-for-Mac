import SwiftUI

/// Searchable sheet over the upstream example configs (klipper/config).
/// Picking one imports it as a preset, applies it and wires the serial port
/// to the detected board — the whole "start from a real config" flow.
struct LibrarySheet: View {
    @EnvironmentObject private var model: AppModel
    @Binding var isPresented: Bool
    @State private var all: [String] = []
    @State private var search = ""
    @State private var loading = true
    @State private var didLoad = false

    private var filtered: [String] {
        search.isEmpty ? all : all.filter { $0.localizedCaseInsensitiveContains(search) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Printer configs").font(.headline)
                Spacer()
                Button("Close") { isPresented = false }
            }
            .padding(12)

            TextField("Search — try \"skr\", \"voron\", \"creality\"…", text: $search)
                .textFieldStyle(.roundedBorder)
                .padding(.horizontal, 12)
                .padding(.bottom, 8)

            List {
                if !model.presetNames.isEmpty {
                    Section(header: Text("Your saved configs")) {
                        ForEach(model.presetNames, id: \.self) { name in
                            Button(action: {
                                isPresented = false
                                Task { await model.usePreset(name) }
                            }) {
                                Text(Self.readable(name)).lineLimit(1)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                Section(header: Text("Printer library")) {
                    if loading {
                        Text("Loading…")
                            .foregroundColor(.secondary)
                    } else if filtered.isEmpty {
                        Text("No matches — try a shorter word.")
                            .foregroundColor(.secondary)
                    } else {
                        ForEach(filtered, id: \.self) { name in
                            Button(action: {
                                isPresented = false
                                Task { await model.applyLibrary(name) }
                            }) {
                                Text(Self.readable(name))
                                    .lineLimit(1)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .frame(width: 480, height: 540)
        .onAppear {
            guard !didLoad else { return }
            didLoad = true
            Task {
                let items = await model.libraryConfigs()
                await MainActor.run {
                    all = items
                    loading = false
                }
            }
        }
    }

    /// "generic-bigtreetech-skr-pico-v1.0" reads better spaced out; the exact
    /// name is what the CLI receives.
    static func readable(_ name: String) -> String {
        name.replacingOccurrences(of: "-", with: " ")
    }
}
