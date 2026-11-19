import AppKit
import SwiftUI

// MARK: - shared 10.15-safe pieces

/// `ProgressView` needs macOS 11+; this NSProgressIndicator wrapper covers
/// every deployment target the app supports.
struct ActivityIndicator: NSViewRepresentable {
    func makeNSView(context: Context) -> NSProgressIndicator {
        let view = NSProgressIndicator()
        view.style = .spinning
        view.controlSize = .small
        view.startAnimation(nil)
        return view
    }

    func updateNSView(_ nsView: NSProgressIndicator, context: Context) {}
}

/// Determinate progress bar (0...1), 10.15-safe stand-in for `ProgressView(value:)`.
struct CompatProgressBar: NSViewRepresentable {
    let value: Double

    func makeNSView(context: Context) -> NSProgressIndicator {
        let view = NSProgressIndicator()
        view.isIndeterminate = false
        view.style = .bar
        view.minValue = 0
        view.maxValue = 1
        view.doubleValue = value
        return view
    }

    func updateNSView(_ nsView: NSProgressIndicator, context: Context) {
        nsView.isIndeterminate = false
        nsView.doubleValue = min(max(value, 0), 1)
    }
}

/// Monospaced, selectable, auto-scrolling log text. SwiftUI's
/// ScrollViewReader requires macOS 11+, so streaming/scrollback panes reuse a
/// plain NSTextView (available since 10.13) instead.
struct LogTextView: NSViewRepresentable {
    let text: String
    var follow: Bool

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = NSColor.textBackgroundColor
        scroll.borderType = .noBorder

        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 10, height: 10))
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.textContainerInset = NSSize(width: 6, height: 6)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude,
                                  height: CGFloat.greatestFiniteMagnitude)

        scroll.documentView = textView
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let textView = scroll.documentView as? NSTextView else { return }
        if textView.string != text {
            textView.string = text
        }
        if follow {
            textView.scrollToEndOfDocument(nil)
        }
    }
}

/// Selectable single-line/multi-line text stand-in for `.textSelection(.enabled)`.
struct SelectableText: NSViewRepresentable {
    let text: String
    var monospaced: Bool = false
    var fontSize: CGFloat = 11

    func makeNSView(context: Context) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.isSelectable = true
        field.isEditable = false
        field.drawsBackground = false
        field.isBezeled = false
        field.lineBreakMode = .byCharWrapping
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.font = monospaced
            ? NSFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
            : NSFont.systemFont(ofSize: NSFont.systemFontSize)
        return field
    }

    func updateNSView(_ nsView: NSTextField, context: Context) {
        if nsView.stringValue != text { nsView.stringValue = text }
        nsView.font = monospaced
            ? NSFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
            : NSFont.systemFont(ofSize: NSFont.systemFontSize)
    }
}

/// `Color(nsColor:)` / `.help` need macOS 11/12+; this keeps tooltips on
/// modern macOS and degrades silently on 10.15.
extension View {
    @ViewBuilder
    func helpIfAvailable(_ text: String) -> some View {
        if #available(macOS 11.0, *) {
            self.help(text)
        } else {
            self
        }
    }

    /// 10.15-safe stand-in for bordered/prominent button styling used in a few places.
    func compatBorderedProminent(tint: Color = .accentColor) -> some View {
        self
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 6).fill(tint.opacity(0.18)))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(tint.opacity(0.45), lineWidth: 1))
    }

    func compatBordered() -> some View {
        self
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25), lineWidth: 1))
    }
}

func compatNSColor(_ color: NSColor) -> Color {
    Color(color)
}

/// Clickable control that never draws the macOS focus ring/highlight box.
/// Used for small glyph actions (e.g. Refresh) where the blue focus ring is noisy.
struct NoFocusButton<Label: View>: NSViewRepresentable {
    let help: String
    let label: Label
    let action: () -> Void

    init(help: String = "", @ViewBuilder label: () -> Label, action: @escaping () -> Void) {
        self.help = help
        self.label = label()
        self.action = action
    }

    func makeCoordinator() -> Coordinator { Coordinator(action: action) }

    func makeNSView(context: Context) -> NSButton {
        let button = NSButton()
        button.title = ""
        button.bezelStyle = .regularSquare
        button.isBordered = false
        button.setButtonType(.momentaryChange)
        button.focusRingType = .none
        button.target = context.coordinator
        button.action = #selector(Coordinator.invoke)
        button.toolTip = help.isEmpty ? nil : help

        let host = NSHostingView(rootView: label)
        host.translatesAutoresizingMaskIntoConstraints = false
        button.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: button.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: button.trailingAnchor),
            host.topAnchor.constraint(equalTo: button.topAnchor),
            host.bottomAnchor.constraint(equalTo: button.bottomAnchor),
        ])
        return button
    }

    func updateNSView(_ nsView: NSButton, context: Context) {
        context.coordinator.action = action
        nsView.toolTip = help.isEmpty ? nil : help
        if let host = nsView.subviews.compactMap({ $0 as? NSHostingView<Label> }).first {
            host.rootView = label
        }
    }

    final class Coordinator: NSObject {
        var action: () -> Void
        init(action: @escaping () -> Void) { self.action = action }
        @objc func invoke() { action() }
    }
}

/// Notion-style card section used by Settings and Dashboard.
struct Card<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color.primary.opacity(0.05))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(Color.secondary.opacity(0.15), lineWidth: 1)
        )
    }
}

/// Rounded label + value row (macOS 10.15 stand-in for `LabeledContent`).
struct FieldRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundColor(.primary)
            Spacer()
            Text(value)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.trailing)
                .lineLimit(2)
        }
    }
}

/// Label + custom trailing content (10.15-safe `LabeledContent` replacement).
struct LabeledRow<Content: View>: View {
    let label: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label).foregroundColor(.primary)
            Spacer()
            content()
        }
    }
}

// MARK: - app components

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
                .font(.system(size: NSFont.systemFontSize, weight: .medium))
        }
        .foregroundColor(.primary)
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
                    .foregroundColor(.secondary)
                    .lineLimit(6)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(compatNSColor(.controlBackgroundColor))
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
            .helpIfAvailable("Click to open in your browser")
        }
    }
}