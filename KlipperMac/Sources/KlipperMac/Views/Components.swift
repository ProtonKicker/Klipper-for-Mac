import SwiftUI

/// Icon + title for a bordered button that lives in a grouped Form row.
///
/// A plain `Label` in that position is treated as a labelled row rather than as
/// button content, and its title truncates even when the row has ~200pt of slack
/// ("Install sta…", "GitH…"). An `HStack` is not, so buttons use this instead.
struct ButtonLabel: View {
    let title: String
    let symbol: String

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: symbol)
            Text(title)
        }
    }
}

struct StatusBadge: View {
    let overall: StackSnapshot.Overall

    var body: some View {
        let color: Color = overall == .running ? Tint.ok
            : (overall == .notResponding ? Tint.attention : Tint.inkSecondary)
        let glyph = overall == .running ? "checkmark.circle.fill"
            : (overall == .notResponding ? "exclamationmark.triangle.fill" : "minus.circle.fill")
        let text = overall == .running ? "Running"
            : (overall == .notResponding ? "Not responding" : "Stopped")
        HStack(spacing: 5) {
            Image(systemName: glyph)
                .foregroundStyle(color)
            Text(text)
                .font(.callout.weight(.semibold))
                .foregroundStyle(Tint.ink)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .background(Capsule().fill(color.opacity(0.14)))
        .overlay(Capsule().strokeBorder(color.opacity(0.5), lineWidth: 1))
    }
}

/// Compact status pill. The CLI's full output is usually several lines of URLs that
/// the page already shows, so a success says only "Done"; the whole message stays
/// available on hover, and a failure keeps one line of detail because "Failed" alone
/// is not actionable.
struct MessageBanner: View {
    @ObservedObject var notifier: Notifier

    var body: some View {
        if let m = notifier.message {
            HStack(spacing: 6) {
                Image(systemName: notifier.isError
                      ? "xmark.octagon.fill" : "checkmark.circle.fill")
                    .foregroundStyle(notifier.isError ? Tint.down : Tint.ok)
                Text(notifier.isError ? "Failed" : "Done")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Tint.ink)
                if notifier.isError {
                    Text(m.replacingOccurrences(of: "\n", with: " "))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                Button {
                    notifier.message = nil
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Dismiss")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(.regularMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1))
            .help(m)
            .padding(12)
            // Sized to sit under the first column only, so a toast never covers the
            // other columns' content.
            .frame(maxWidth: 340, alignment: .leading)
        }
    }
}

/// URL row — clicking copies the address to the clipboard. `model` is passed by
/// reference on purpose: this view must not observe it, or every 2s poll would
/// re-render every row on the page.
struct CopyableURL: View {
    let model: AppModel
    let label: String
    let url: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "network")
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Tint.network)
            Text(label)
            Spacer()
            Button(url) {
                model.copyURL(url)
            }
            .buttonStyle(.link)
            .lineLimit(1)
            .truncationMode(.middle)
            .help("Click to copy the address")
        }
    }
}
