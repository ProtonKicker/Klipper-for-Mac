import SwiftUI

/// The page's only colour authority. Five semantic system hues plus the user's own
/// accent, all appearance-adaptive. Literal RGB is deliberately absent: it does not
/// follow Dark Mode, and the same orange measured 1.81:1 on light against 5.99:1 on dark.
///
/// Two laws hold everywhere this file is used: hue never carries a number (values are
/// always `ink`), and hue is never the only signal — every status colour is paired with
/// a glyph and a word, because the previous 8px red/green dot was unreadable to
/// deuteranopes.
enum Tint {
    static let ok = Color(nsColor: .systemGreen)
    static let down = Color(nsColor: .systemRed)
    static let attention = Color(nsColor: .systemOrange)
    static let heat = Color(nsColor: .systemYellow)
    static let network = Color(nsColor: .systemBlue)

    static let ink = Color(nsColor: .labelColor)
    static let inkSecondary = Color(nsColor: .secondaryLabelColor)
}
