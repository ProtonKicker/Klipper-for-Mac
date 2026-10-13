import AppKit
import SwiftUI

/// The whole visual language of the app lives here: a flat white page,
/// hairline-divided tables, pastel property chips, one Notion blue.
/// No cards, no shadows, no gradients. Literal RGB exists nowhere else.
enum Skin {
    private static func hex(_ rgb: UInt32, _ a: Double = 1) -> Color {
        Color(.sRGB, red: Double((rgb >> 16) & 0xFF) / 255.0,
              green: Double((rgb >> 8) & 0xFF) / 255.0,
              blue: Double(rgb & 0xFF) / 255.0, opacity: a)
    }

    // surfaces — the page IS the surface
    static let page = Color.white
    static let well = hex(0xF7F6F3)           // recessed insets (log blocks)
    static let rowHover = Color.black.opacity(0.025)
    static let tableLine = hex(0xE7E6E4)      // hairline row/column dividers
    static let trackFill = Color.black.opacity(0.08)

    // text
    static let ink = hex(0x191919)
    static let inkSecondary = hex(0x6B6A68)
    static let inkMuted = hex(0x9B9A97)

    // the one accent
    static let accent = hex(0x2383E2)
    static let accentHover = hex(0x1A6FC0)
    static let onAccent = Color.white

    // Notion property-chip palette — pastel fill, same-hue dark ink
    static let chipGrayBG = hex(0xF1F1EF)
    static let chipGrayInk = hex(0x757370)
    static let chipGreenBG = hex(0xDFEFE3)
    static let chipGreenInk = hex(0x33604A)
    static let chipBlueBG = hex(0xDCEBFA)
    static let chipBlueInk = hex(0x2A5B85)
    static let chipYellowBG = hex(0xFAEFD2)
    static let chipYellowInk = hex(0x8A651F)
    static let chipRedBG = hex(0xFBE3E0)
    static let chipRedInk = hex(0x9E3B31)
}

// MARK: - chip

/// A pastel property tag — Notion's status vocabulary.
struct Chip: View {
    let text: String
    let bg: Color
    let ink: Color

    var body: some View {
        Text(text)
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(ink)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(RoundedRectangle(cornerRadius: 4).fill(bg))
            .fixedSize()
    }
}

/// Stack status as a chip pair: word + hue.
struct StatusChip: View {
    let overall: StackSnapshot.Overall
    var body: some View {
        switch overall {
        case .running: Chip(text: "Running", bg: Skin.chipGreenBG, ink: Skin.chipGreenInk)
        case .notResponding: Chip(text: "Not responding", bg: Skin.chipRedBG, ink: Skin.chipRedInk)
        case .stopped: Chip(text: "Stopped", bg: Skin.chipGrayBG, ink: Skin.chipGrayInk)
        }
    }
}

// MARK: - table

/// A database block: gray icon+title header, then hairline-divided rows.
struct NotionSection<Accessory: View, Content: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder var accessory: () -> Accessory
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: symbol)
                    .font(.system(size: 12))
                    .foregroundStyle(Skin.inkMuted)
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Skin.inkSecondary)
                    .fixedSize()
                Spacer(minLength: 8)
                accessory()
            }
            .padding(.horizontal, 8)
            .frame(height: 28)
            VStack(spacing: 0) { content() }
        }
    }
}

extension NotionSection where Accessory == EmptyView {
    init(title: String, symbol: String,
         @ViewBuilder content: @escaping () -> Content) {
        self.init(title: title, symbol: symbol,
                  accessory: { EmptyView() }, content: content)
    }
}

/// One table row: leading glyph, title, a vertical property divider,
/// then the property cell. Full row highlights on hover.
struct TRow<Trailing: View>: View {
    let symbol: String
    let title: String
    var propertyWidth: CGFloat = 150
    @ViewBuilder var trailing: () -> Trailing
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 12))
                .foregroundStyle(Skin.inkMuted)
                .frame(width: 16)
            Text(title)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(Skin.ink)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 12)
            Rectangle()
                .fill(Skin.tableLine)
                .frame(width: 1)
            HStack(spacing: 8) { trailing() }
                .frame(width: propertyWidth, alignment: .leading)
        }
        .padding(.horizontal, 8)
        .frame(maxWidth: .infinity)
        .frame(height: 44)
        .background(hovering ? Skin.rowHover : Color.clear)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
    }
}

/// A plain full-width row (no property column) for notes and empty states.
struct TInfoRow: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(size: 13))
            .foregroundStyle(Skin.inkMuted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 8)
            .padding(.vertical, 12)
    }
}

struct TableDivider: View {
    var body: some View {
        Rectangle()
            .fill(Skin.tableLine)
            .frame(height: 1)
            .padding(.horizontal, 8)
    }
}

// MARK: - controls

enum NButtonVariant { case gray, blue, red }

/// Notion's button: flat, radius 6, no border, subtle hover darkening.
struct NotionButton: ButtonStyle {
    var variant: NButtonVariant = .gray
    var small = false

    func makeBody(configuration: Configuration) -> some View {
        Styled(configuration: configuration, variant: variant, small: small)
    }

    private struct Styled: View {
        @Environment(\.isEnabled) private var isEnabled
        let configuration: Configuration
        let variant: NButtonVariant
        let small: Bool
        @State private var hovering = false

        var body: some View {
            configuration.label
                .font(.system(size: small ? 12 : 13, weight: .medium))
                .foregroundStyle(fg)
                .padding(.horizontal, small ? 9 : 12)
                .frame(height: small ? 24 : 28)
                .background(RoundedRectangle(cornerRadius: 5).fill(bg))
                .scaleEffect(configuration.isPressed ? 0.98 : 1)
                .opacity(isEnabled ? 1 : 0.4)
                .contentShape(RoundedRectangle(cornerRadius: 5))
                .onHover { hovering = $0 }
                .animation(.easeOut(duration: 0.12), value: hovering)
        }

        private var fg: Color {
            switch variant {
            case .gray: return Skin.ink
            case .blue: return Skin.onAccent
            case .red: return Skin.chipRedInk
            }
        }
        private var bg: Color {
            switch variant {
            case .gray: return hovering ? hex(0xE8E7E5) : hex(0xF1F1EF)
            case .blue: return hovering ? Skin.accentHover : Skin.accent
            case .red: return hovering ? hex(0xF8D5D1) : Skin.chipRedBG
            }
        }
        private func hex(_ rgb: UInt32) -> Color {
            Color(.sRGB, red: Double((rgb >> 16) & 0xFF) / 255.0,
                  green: Double((rgb >> 8) & 0xFF) / 255.0,
                  blue: Double(rgb & 0xFF) / 255.0)
        }
    }
}

/// Bare gray text button — Notion's inline affordance ("Move", "Reveal").
struct TextButton: View {
    let text: String
    var enabled: Bool = true
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(hovering ? Skin.ink : Skin.inkSecondary)
                .background(hovering ? Skin.hexRowHover : Color.clear,
                            in: RoundedRectangle(cornerRadius: 4))
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .opacity(enabled ? 1 : 0.4)
        .disabled(!enabled)
    }
}

extension Skin {
    static let hexRowHover = Color.black.opacity(0.05)
}

// MARK: - type

enum AppInfo {
    static var version: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }
}

extension Font {
    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

