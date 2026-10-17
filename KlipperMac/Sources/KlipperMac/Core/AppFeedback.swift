import Foundation

enum AppFeedback {
    static func bannerMessage(_ text: String, limit: Int = 400) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return String(trimmed.prefix(limit))
    }

    static func firstLine(_ text: String) -> String {
        for raw in text.split(whereSeparator: \.isNewline) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if !line.isEmpty { return line }
        }
        return ""
    }

    static func cliNotFoundInstallMessage(_ installCommand: String) -> String {
        "Klipper CLI not found — install it first:\n" + installCommand
    }

    static func camToast(action: String, output: String) -> String {
        let first = firstLine(output)
        switch action {
        case "on":
            if first.contains("ask once") {
                return "macOS is asking for camera access — click OK."
            }
            return output.contains("restart")
                ? "Camera is on — restart the stack once to see it in the web UI."
                : "Camera is on"
        case "off":
            return "Camera is off"
        case "snapshot":
            return first.isEmpty ? "Saved snapshot" : first
        case "config":
            return "Saved — turn the camera off and on to apply."
        default:
            return first.isEmpty ? "Done" : first
        }
    }
}
