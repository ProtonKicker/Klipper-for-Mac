import Foundation

enum SettingsPresentation {
    static func pinText(_ pins: [String: String], component: String) -> String {
        let pin = pins[component] ?? "?"
        let isSHA = pin.count == 40 && pin.allSatisfy { $0.isHexDigit }
        if let sha = pins[component + "_sha"], !sha.isEmpty {
            return (isSHA ? String(sha.prefix(7)) : pin) + "  \u{00B7}  " + String(sha.prefix(7))
        }
        return isSHA ? String(pin.prefix(7)) + " (commit)" : pin
    }
}
