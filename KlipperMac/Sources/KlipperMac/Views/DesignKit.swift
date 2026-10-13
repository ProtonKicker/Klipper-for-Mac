import AppKit
import SwiftUI

/// The whole visual language of the app lives here: light-only surfaces, one
/// blue accent, hairlines instead of borders, and two featherweight shadows.
/// Literal RGB exists nowhere else.
enum Skin {
    private static func hex(_ rgb: UInt32, _ a: Double = 1) -> Color {
        Color(.sRGB, red: Double((rgb >> 16) & 0xFF) / 255.0,
              green: Double((rgb >> 8) & 0xFF) / 255.0,
              blue: Double(rgb & 0xFF) / 255.0, opacity: a)
    }

    // surfaces
    static let washTop = hex(0xF8F7F4)
    static let washBottom = hex(0xF0F0EE)
    static let card = Color.white
    static let well = hex(0xF5F4F1)          // recessed insets (console, inputs)
    static let hairline = Color.black.opacity(0.06)
    static let hairlineSoft = Color.black.opacity(0.04)
    static let specular = Color.white.opacity(0.75)
    static let shadowSoft = Color.black.opacity(0.04)
    static let shadowLift = Color.black.opacity(0.03)
    static let trackFill = Color.black.opacity(0.05)
    static let gridLine = Color.black.opacity(0.05)

    // text
    static let ink = hex(0x1D1C1A)
    static let inkSecondary = hex(0x6E6A66)
    static let inkMuted = hex(0xA39E98)

    // the one accent
    static let accent = hex(0x0071E3)
    static let accentHover = hex(0x0058B8)
    static let accentFill = hex(0xEAF3FD)
    static let onAccent = Color.white

    // semantic status — dots and tints only, never whole surfaces
    static let ok = Color(.systemGreen)
    static let alert = Color(.systemRed)
    static let warn = Color(.systemOrange)

    static func tint(_ c: Color, _ a: Double = 0.12) -> Color { c.opacity(a) }
}

// MARK: - card

/// A solid white card floating on the wash: hairline edge, a faint specular
/// line along the top edge, two soft shadows. Content sits on cards; cards
/// sit on the page — never cards on cards.
struct GlassCard<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) { content() }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .padding(16)
            .background(Skin.card)
            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(Skin.hairline, lineWidth: 1))
            .overlay(alignment: .top) {
                LinearGradient(colors: [.clear, Skin.specular, .clear],
                               startPoint: .leading, endPoint: .trailing)
                    .frame(height: 1)
                    .padding(.horizontal, 30)
            }
            .shadow(color: Skin.shadowSoft, radius: 3, y: 1)
            .shadow(color: Skin.shadowLift, radius: 14, y: 6)
    }
}

// MARK: - controls

enum AppleButtonVariant { case bordered, filled, danger }

struct AppleButton: ButtonStyle {
    var variant: AppleButtonVariant = .bordered
    var small = false

    func makeBody(configuration: Configuration) -> some View {
        Styled(configuration: configuration, variant: variant, small: small)
    }

    private struct Styled: View {
        @Environment(\.isEnabled) private var isEnabled
        let configuration: Configuration
        let variant: AppleButtonVariant
        let small: Bool
        @State private var hovering = false

        var body: some View {
            configuration.label
                .font(.system(size: small ? 12 : 13, weight: .medium))
                .foregroundStyle(fg)
                .padding(.horizontal, small ? 10 : 14)
                .frame(height: small ? 26 : 30)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(bg))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(border, lineWidth: 1))
                .scaleEffect(configuration.isPressed ? 0.98 : 1)
                .opacity(isEnabled ? 1 : 0.4)
                .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .onHover { hovering = $0 }
                .animation(.easeOut(duration: 0.15), value: hovering)
        }

        private var fg: Color {
            switch variant {
            case .bordered: return Skin.ink
            case .filled: return Skin.onAccent
            case .danger: return Skin.alert
            }
        }
        private var bg: Color {
            switch variant {
            case .bordered: return hovering ? Skin.well : Skin.card
            case .filled: return hovering ? Skin.accentHover : Skin.accent
            case .danger: return hovering ? Skin.tint(Skin.alert, 0.07) : Skin.card
            }
        }
        private var border: Color {
            switch variant {
            case .bordered: return Skin.hairline
            case .filled: return bg
            case .danger: return Skin.tint(Skin.alert, 0.3)
            }
        }
    }
}

/// Round icon-only button for the header chrome.
struct IconButton: View {    let symbol: String
    var help: String? = nil
    var tint: Color? = nil
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(tint ?? Skin.inkSecondary)
                .frame(width: 28, height: 28)
                .background(Circle().fill(Skin.card))
                .overlay(Circle().strokeBorder(Skin.hairline, lineWidth: 1))
                .shadow(color: Skin.shadowSoft, radius: 2, y: 1)
        }
        .buttonStyle(.plain)
        .help(help ?? symbol)
    }
}

// MARK: - type

/// 1px separator inside cards.
struct Hairline: View {
    var body: some View {
        Rectangle()
            .fill(Skin.hairlineSoft)
            .frame(height: 1)
    }
}

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

/// Uppercase micro label — the only "heading" a card gets.
struct MicroLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.9)
            .foregroundStyle(Skin.inkMuted)
            .fixedSize()
    }
}

/// Quiet inline status: a small dot and a gray word beside the title.
/// No capsule, no tinted fill — the hue is a whisper, the word carries it.
struct StatusPill: View {
    let overall: StackSnapshot.Overall
    var body: some View {
        let color: Color = overall == .running ? Skin.ok
            : (overall == .notResponding ? Skin.alert : Skin.inkMuted)
        let text = overall == .running ? "Running"
            : (overall == .notResponding ? "Not responding" : "Stopped")
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 5, height: 5)
            Text(text)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Skin.inkSecondary)
        }
    }
}

// MARK: - formatting

enum Fmt {
    static func remaining(_ seconds: Double) -> String {
        let m = Int((seconds / 60).rounded())
        if m >= 60 { return "\(m / 60) hr \(m % 60) min" }
        if m >= 1 { return "\(m) min" }
        return "under a minute"
    }

    static func clock(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}
