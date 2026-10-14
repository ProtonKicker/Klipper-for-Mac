import SwiftUI

/// Searchable sheet over the upstream example configs (klipper/config).
/// Picking one imports it as a preset, applies it and wires the serial port
/// to the detected board — the whole "start from a real config" flow.
struct LibrarySheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var all: [String] = []
    @State private var search = ""
    @State private var loading = true

    private var filtered: [String] {
        search.isEmpty ? all : all.filter { $0.localizedCaseInsensitiveContains(search) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Printer configs").font(.headline)
                Spacer()
                Button("Close") { dismiss() }
            }
            .padding(12)

            TextField("Search — try \"skr\", \"voron\", \"creality\"…", text: $search)
                .textFieldStyle(.roundedBorder)
                .padding(.horizontal, 12)
                .padding(.bottom, 8)

            List {
                if !model.presetNames.isEmpty {
                    Section("Your saved configs") {
                        ForEach(model.presetNames, id: \.self) { name in
                            Button {
                                dismiss()
                                Task { await model.usePreset(name) }
                            } label: {
                                Text(Self.readable(name)).lineLimit(1)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                Section("Printer library") {
                    if loading {
                        Text("Loading…")
                            .foregroundStyle(.secondary)
                    } else if filtered.isEmpty {
                        Text("No matches — try a shorter word.")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(filtered, id: \.self) { name in
                            Button {
                                dismiss()
                                Task { await model.applyLibrary(name) }
                            } label: {
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
        .task {
            all = await model.libraryConfigs()
            loading = false
        }
    }

    /// "generic-bigtreetech-skr-pico-v1.0" reads better spaced out; the exact
    /// name is what the CLI receives.
    static func readable(_ name: String) -> String {
        name.replacingOccurrences(of: "-", with: " ")
    }
}
