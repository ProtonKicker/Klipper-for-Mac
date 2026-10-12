import SwiftUI

/// Shared formatting + small chrome for the dashboard panels.
enum Fmt {
    static func duration(_ seconds: Double) -> String {
        let s = Int(seconds)
        if s >= 3600 { return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) }
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    static func fileSize(_ bytes: Double) -> String {
        if bytes >= 1_048_576 { return String(format: "%.1f MB", bytes / 1_048_576) }
        if bytes >= 1_024 { return String(format: "%.0f KB", bytes / 1_024) }
        return "\(Int(bytes)) B"
    }

    static func date(_ modified: Double) -> String {
        let d = Date(timeIntervalSince1970: modified)
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f.string(from: d)
    }
}

struct StateChip: View {
    let text: String
    let color: Color
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(text).font(.caption.weight(.medium))
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5))
    }
}

/// Icon tile like System Settings rows: colored rounded square + glyph.
struct IconTile: View {
    let symbol: String
    let tint: Color
    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: 22, height: 22)
            .background(RoundedRectangle(cornerRadius: 5).fill(tint.gradient))
    }
}

/// Card container for side-by-side dashboard panels; `plain` mode drops the
/// card chrome so a full-page panel fills the detail pane edge-to-edge.
struct Panel<Trailing: View, Content: View>: View {
    let title: String
    let icon: String
    let tint: Color
    var expands = false
    var plain = false
    let trailing: Trailing
    let content: Content

    init(_ title: String, icon: String = "square", tint: Color = .accentColor,
         expands: Bool = false, plain: Bool = false,
         @ViewBuilder trailing: () -> Trailing = { EmptyView() },
         @ViewBuilder content: () -> Content) {
        self.title = title
        self.icon = icon
        self.tint = tint
        self.expands = expands
        self.plain = plain
        self.trailing = trailing()
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                IconTile(symbol: icon, tint: tint)
                Text(title).font(plain ? .title3.weight(.semibold) : .headline)
                Spacer()
                trailing
            }
            content
        }
        .padding(.vertical, 16)
        .padding(.horizontal, plain ? 20 : 16)
        .frame(maxWidth: .infinity,
               maxHeight: (expands || plain) ? .infinity : nil,
               alignment: .topLeading)
        .background(plain ? Color.clear : Color(nsColor: .textBackgroundColor))
        .shadow(color: plain ? .clear : .black.opacity(0.06), radius: 3, y: 1)
        .overlay {
            if !plain {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(Color(nsColor: .separatorColor).opacity(0.6),
                                  lineWidth: 0.5)
            }
        }
    }
}
