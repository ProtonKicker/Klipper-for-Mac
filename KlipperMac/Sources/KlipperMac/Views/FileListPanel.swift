import SwiftUI

struct FileListPanel: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Panel("G-Code Files", icon: "doc.text.fill", tint: .blue, expands: true, plain: true) {
            if model.uploading {
                ProgressView().controlSize(.small)
            }
            Button {
                model.uploadGcode()
            } label: {
                Image(systemName: "square.and.arrow.up")
            }
            .buttonStyle(.plain)
            .help("Upload g-code file")
            Button {
                Task { await model.refreshFiles() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .help("Reload file list")
        } content: {
            if model.gcodeFiles.isEmpty {
                Text("No g-code files — upload one here or drop it into the gcodes folder.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    LazyVStack(spacing: 2) {
                        ForEach(model.gcodeFiles) { file in
                            HStack(spacing: 8) {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(file.filename)
                                        .lineLimit(1)
                                    Text("\(Fmt.fileSize(file.size))   \(Fmt.date(file.modified))")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Print") { model.startPrint(file) }
                                    .buttonStyle(.bordered)
                                    .controlSize(.small)
                            }
                            .padding(.vertical, 3)
                            .padding(.horizontal, 4)
                            Divider()
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}
