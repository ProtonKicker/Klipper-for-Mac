import Foundation

struct SerialPort: Identifiable, Hashable {
    let path: String
    let label: String
    var id: String { path }
}

/// Mirrors detect.py: macOS exposes ports as /dev/cu.<name> files, the driver
/// family is embedded in the name.
enum SerialScanner {
    private static let patterns: [(prefix: String, label: String)] = [
        ("usbserial", "FTDI (or clone)"),
        ("usbmodem", "USB-CDC (STM32 / SKR / Pi Pico class boards)"),
        ("wchusbserial", "WCH CH340/CH341"),
        ("SLAB_USBtoUART", "Silicon Labs CP210x"),
        ("usbtrance", "Teensy HID"),
    ]

    static func scan() -> [SerialPort] {
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: "/dev")
        else { return [] }
        var found: [SerialPort] = []
        for name in entries where name.hasPrefix("cu.") {
            let bare = String(name.dropFirst(3))
            // longest label first: wchusbserial also starts with usbserial? no —
            // it starts with "wch", but check every prefix match explicitly.
            for pat in patterns where bare.hasPrefix(pat.prefix) {
                found.append(SerialPort(path: "/dev/" + name, label: pat.label))
                break
            }
        }
        return found.sorted { $0.path < $1.path }
    }
}
