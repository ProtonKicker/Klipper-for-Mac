import SwiftUI

struct StatusBadge: View {
    let overall: StackSnapshot.Overall

    var body: some View {
        let color: Color = overall == .running ? .green
            : (overall == .notResponding ? .orange : .secondary)
        let text = overall == .running ? "Running"
            : (overall == .notResponding ? "Not responding" : "Stopped")
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(text)
                .font(.callout.weight(.medium))
        }
        .foregroundStyle(.primary)
        .fixedSize()
    }
}

struct MessageBanner: View {
    @ObservedObject var notifier: Notifier

    var body: some View {
        if let m = notifier.message {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    FlatIcon(kind: notifier.isError ? .warning : .check, size: 14,
                             color: notifier.isError ? FlatPalette.amber : FlatPalette.green)
                    Text(notifier.isError ? "Problem" : "Done")
                        .font(.caption.bold())
                    Spacer()
                    Button {
                        notifier.message = nil
                    } label: {
                        FlatIcon(kind: .close, size: 13, color: Color.secondary)
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

/// URL row — clicking opens the address in the default browser.
struct BrowserLink: View {
    let label: String
    let url: String
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack {
            Text(label)
            Spacer()
            Button(url) {
                model.openURL(url)
            }
            .buttonStyle(.link)
            .help("Click to open in your browser")
        }
    }
}
