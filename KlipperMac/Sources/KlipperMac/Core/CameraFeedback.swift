import Foundation

enum CameraStateKind: Equatable {
    case on
    case off
    case unknown
}

struct CameraStatePresentation: Equatable {
    let text: String
    let kind: CameraStateKind
}

struct CameraDeviceListPresentation: Equatable {
    let devices: [String]
    let hint: String
}

enum CameraFeedback {
    static let listNoCLI = "Klipper CLI not found — install it first."
    static let listFailed = "Camera list not ready — press Turn on camera, then Refresh."
    static let noCameraFound = "No camera found — plug one in, or use the camera built into this Mac"
    static let guidance: Set<String> = [listNoCLI, listFailed]

    static func statePresentation(isOnline: Bool?) -> CameraStatePresentation {
        switch isOnline {
        case .some(true):
            return CameraStatePresentation(text: "Camera is on", kind: .on)
        case .some(false):
            return CameraStatePresentation(text: "Camera is off", kind: .off)
        case .none:
            return CameraStatePresentation(text: "Camera status unknown — press Refresh",
                                           kind: .unknown)
        }
    }

    static func placeholderText(isCLIInstalled: Bool, camOnline: Bool?) -> String {
        if !isCLIInstalled { return "Install the Klipper CLI to use the camera." }
        return camOnline == nil
            ? "Press Refresh to check the camera."
            : "Turn the camera on to see the live picture."
    }

    /// The helper prints `index<TAB>name<TAB>size<TAB>fps` for real devices.
    /// Guidance lines can be mixed into the same stream, so only rows whose
    /// first field is an integer index count as devices.
    static func deviceNames(fromListOutput text: String) -> [String] {
        var names: [String] = []
        var seen = Set<String>()
        for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
            let fields = raw.split(separator: "\t", omittingEmptySubsequences: false)
                .map { $0.trimmingCharacters(in: .whitespaces) }
            guard fields.count >= 2, Int(fields[0]) != nil else { continue }
            let name = fields[1]
            guard !name.isEmpty, seen.insert(name).inserted else { continue }
            names.append(name)
        }
        return names
    }

    static func deviceListPresentation(lines: [String], selectedDevice: String) -> CameraDeviceListPresentation {
        let names = lines.filter { !guidance.contains($0) }
        let devices: [String]
        if !selectedDevice.isEmpty, !names.contains(selectedDevice) {
            devices = [selectedDevice] + names
        } else {
            devices = names
        }
        let hint = names.isEmpty
            ? (lines.first(where: { guidance.contains($0) }) ?? noCameraFound)
            : ""
        return CameraDeviceListPresentation(devices: devices, hint: hint)
    }
}
