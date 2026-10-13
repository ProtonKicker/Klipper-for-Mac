import SwiftUI

/// Icon + title for a button label. An `HStack` (rather than a `Label`) is used
/// because in some containers a `Label` is treated as a labelled row and its
/// title truncates even when there is slack.
struct ButtonLabel: View {
    let title: String
    let symbol: String

    var body: some View {
        HStack(spacing: 7) {
            Image(systemName: symbol)
            Text(title)
        }
    }
}

/// Status pill: a dot and a word. Hue never carries the meaning alone —
/// the word always states it, so the pill survives colour-blind viewing.
struct StatusPill: View {
    let overall: StackSnapshot.Overall

    var body: some View {
        let color: Color = overall == .running ? Palette.green
            : (overall == .notResponding ? Palette.red : Palette.textMuted)
        let text = overall == .running ? "Running"
            : (overall == .notResponding ? "Not responding" : "Stopped")
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .tracking(0.1)
        }
        .foregroundStyle(color)
        .padding(.leading, 9)
        .padding(.trailing, 12)
        .padding(.vertical, 5)
        .background(Capsule().fill(Palette.tintFill(color)))
        .overlay(Capsule().strokeBorder(Palette.tintFillStrong(color), lineWidth: 1))
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
                    .foregroundStyle(notifier.isError ? Palette.red : Tint.ok)
                Text(notifier.isError ? "Failed" : "Done")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Palette.textPrimary)
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
            .overlay(Capsule().strokeBorder(Palette.border, lineWidth: 1))
            .help(m)
            .padding(12)
            // Matched to the card's width so the toast sits under the panel it reports on.
            .frame(maxWidth: 620, alignment: .leading)
        }
    }
}

/// Mono URL chip — clicking copies the address to the clipboard. `model` is passed
/// by reference on purpose: this view must not observe it, or every 2s poll would
/// re-render every row on the page.
struct LinkChip: View {
    let model: AppModel
    let label: String
    let url: String
    var systemImage: String? = nil
    var dimmed = false
    @State private var hovering = false

    var body: some View {
        Button {
            model.copyURL(url)
        } label: {
            HStack(spacing: 6) {
                if let systemImage {
                    Image(systemName: systemImage)
                        .font(.system(size: 11))
                        .opacity(0.55)
                }
                Text(url.replacingOccurrences(of: "http://", with: ""))
                    .font(.panelMono(11.5))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .fixedSize()
                Image(systemName: "arrow.up.right")
                    .font(.system(size: 10, weight: .medium))
                    .opacity(0.45)
            }
            .foregroundStyle(hovering ? Palette.accent : Palette.textSecondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 4)
                .fill(hovering ? Palette.accentLight : Palette.surface)
                .overlay(RoundedRectangle(cornerRadius: 4)
                    .strokeBorder(hovering ? Palette.accentBorder : Palette.border,
                                  lineWidth: 1)))
            .contentShape(RoundedRectangle(cornerRadius: 4))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .opacity(dimmed ? 0.35 : 1)
        .allowsHitTesting(!dimmed)
        .help("\(label) — click to copy")
    }
}
