import SwiftUI

struct StatusBadge: View {
    let overall: StackSnapshot.Overall
    private var presentation: StatusBadgePresentation {
        AppFeedback.statusBadgePresentation(overall: overall)
    }

    private var color: Color {
        switch presentation.tone {
        case .positive:
            return .green
        case .warning:
            return .orange
        case .neutral:
            return .secondary
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(presentation.text)
                .font(.callout.weight(.medium))
        }
        .foregroundStyle(.primary)
        .fixedSize()
    }
}

struct MessageBanner: View {
    @ObservedObject var notifier: Notifier

    private var banner: BannerPresentation? {
        AppFeedback.bannerPresentation(message: notifier.message,
                                       isError: notifier.isError)
    }

    private var iconColor: Color {
        banner?.tone == .warning ? FlatPalette.amber : FlatPalette.green
    }

    private var iconKind: FlatIconKind {
        banner?.tone == .warning ? .warning : .check
    }

    var body: some View {
        if let banner {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    FlatIcon(kind: iconKind, size: 14, color: iconColor)
                    Text(banner.title)
                        .font(.caption.bold())
                    Spacer()
                    Button {
                        notifier.message = nil
                    } label: {
                        FlatIcon(kind: .close, size: 13, color: Color.secondary)
                    }
                    .buttonStyle(.plain)
                }
                Text(banner.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(6)
                    .textSelection(.enabled)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.secondary.opacity(0.18), lineWidth: 1)
            )
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
