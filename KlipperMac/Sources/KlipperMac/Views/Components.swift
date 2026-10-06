import SwiftUI

struct StatusBadge: View {
    let overall: StackSnapshot.Overall

    var body: some View {
        let color: Color = overall == .running ? .green
            : (overall == .notResponding ? .orange : .secondary)
        let text = overall == .running ? "Running"
            : (overall == .notResponding ? "Not responding" : "Stopped")
        Text(text)
            .font(.headline)
            .foregroundStyle(color)
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
            .background(Capsule().fill(color.opacity(0.14)))
            .overlay(Capsule().strokeBorder(color.opacity(0.5), lineWidth: 1))
    }
}

struct MessageBanner: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if let m = model.message {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 5) {
                    Image(systemName: model.messageIsError
                          ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(model.messageIsError ? Color.orange : Color.green)
                    Text(model.messageIsError ? "Problem" : "Done")
                        .font(.caption.bold())
                    Spacer()
                    Button {
                        model.message = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
                Text(m)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(6)
                    .textSelection(.enabled)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
            .padding(8)
        }
    }
}

/// URL row — clicking copies the address to the clipboard.
struct CopyableURL: View {
    let label: String
    let url: String
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack {
            Text(label)
            Spacer()
            Button(url) {
                model.copyURL(url)
            }
            .buttonStyle(.link)
            .help("Click to copy the address")
        }
    }
}
