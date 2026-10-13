import AppKit
import SwiftUI

/// The page's two colour authorities:
/// - `Tint` — five semantic system hues plus the user's own accent, all
///   appearance-adaptive. Status colours keep their glyph + word pairing,
///   because hue is never the only signal.
/// - `Palette` — the single-panel design tokens, rebuilt from the UI demo's
///   light-only values as dynamic light/dark pairs: exact demo look in light
///   mode, hand-tuned equivalents in dark. This file is the only place where
///   literal RGB exists; every view reads tokens by name.
///
/// Two laws hold everywhere this file is used: hue never carries a number
/// (values are always `ink`), and hue is never the only signal.
enum Tint {
    static let ok = Color(nsColor: .systemGreen)
    static let down = Color(nsColor: .systemRed)
    static let attention = Color(nsColor: .systemOrange)
    static let heat = Color(nsColor: .systemYellow)
    static let network = Color(nsColor: .systemBlue)

    static let ink = Color(nsColor: .labelColor)
    static let inkSecondary = Color(nsColor: .secondaryLabelColor)
}

/// Design tokens for the single panel. Resolve fresh in `body` — never cache a
/// token in `@State` or a long-lived `let`, or it stops following appearance flips.
enum Palette {
    private static func hex(_ rgb: UInt32, _ alpha: Double = 1) -> NSColor {
        NSColor(srgbRed: CGFloat((rgb >> 16) & 0xFF) / 255.0,
                green: CGFloat((rgb >> 8) & 0xFF) / 255.0,
                blue: CGFloat(rgb & 0xFF) / 255.0,
                alpha: alpha)
    }

    private static func dual(light: NSColor, dark: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        })
    }

    // MARK: surfaces
    static let pageBG = dual(light: hex(0xF5F4F1), dark: hex(0x1C1B19))
    static let surface = dual(light: .white, dark: hex(0x262523))
    static let border = dual(light: hex(0xE7E5E4), dark: hex(0x3B3936))
    static let borderSubtle = dual(light: hex(0xF0EEEC), dark: hex(0x32302D))

    // MARK: text
    static let textPrimary = dual(light: hex(0x1C1917), dark: hex(0xEDEAE6))
    static let textSecondary = dual(light: hex(0x78716C), dark: hex(0xA7A19B))
    static let textMuted = dual(light: hex(0xA8A29E), dark: hex(0x8A8580))

    // MARK: accent (indigo)
    static let accent = dual(light: hex(0x6366F1), dark: hex(0x818CF8))
    static let accentHover = dual(light: hex(0x4F46E5), dark: hex(0x6366F1))
    static let accentLight = dual(light: hex(0xEEF2FF), dark: hex(0x2A2A45))
    static let accentBorder = dual(light: hex(0xC7D2FE), dark: hex(0x40406B))
    static let onAccent = Color.white

    // MARK: status
    // Light values are the demo's; dark falls back to the system hues, which
    // stay legible on the dark surface (the demo hexes would fail contrast there).
    static let green = dual(light: hex(0x16A34A), dark: .systemGreen)
    static let red = dual(light: hex(0xDC2626), dark: .systemRed)

    /// Pill/chip fills: a translucent tint over the card surface survives both
    /// appearances without a second literal palette.
    static func tintFill(_ token: Color) -> Color { token.opacity(0.14) }
    static func tintFillStrong(_ token: Color) -> Color { token.opacity(0.5) }
}
