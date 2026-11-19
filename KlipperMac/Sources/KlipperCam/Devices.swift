// Camera listing, picking and privacy status. No permission is requested here
// unless the caller asks for it — `--list` and `--check-permission` must stay
// read-only so they can be used by scripts and by the first-run doctor checks.

import AVFoundation
import Foundation

struct CameraInfo {
    let device: AVCaptureDevice
    let index: Int
    let name: String
    let width: Int
    let height: Int
    let maxFPS: Double
}

func videoDevices() -> [CameraInfo] {
    // macOS 13 has no discovery type covering USB and Continuity cameras, so
    // the legacy listing is the only one that sees everything plugged in.
    var out: [CameraInfo] = []
    for (i, d) in AVCaptureDevice.devices(for: .video).enumerated() {
        let best = topFormat(d)
        var fps = 0.0
        for r in best.videoSupportedFrameRateRanges { fps = max(fps, r.maxFrameRate) }
        let dims = best.formatDescription.dimensions
        out.append(CameraInfo(device: d, index: i, name: d.localizedName,
                              width: Int(dims.width), height: Int(dims.height),
                              maxFPS: fps))
    }
    return out
}

/// The biggest mode this camera can do. Used for `--list`, and as the fallback
/// when the requested size does not exist.
func topFormat(_ device: AVCaptureDevice) -> AVCaptureDevice.Format {
    var best = device.activeFormat
    var bestArea = area(best)
    for f in device.formats {
        if area(f) > bestArea {
            best = f
            bestArea = area(f)
        }
    }
    return best
}

func area(_ format: AVCaptureDevice.Format) -> Int {
    let d = format.formatDescription.dimensions
    return Int(d.width) * Int(d.height)
}

/// `--device` accepts an index from `--list`, a full name, or a piece of one.
/// Empty means: first built-in camera, otherwise the first camera at all.
func selectDevice(_ list: [CameraInfo], _ spec: String) -> AVCaptureDevice? {
    if spec.isEmpty {
        if let builtIn = list.first(where: {
            $0.device.deviceType == .builtInWideAngleCamera
                || $0.device.uniqueID.contains("unicam") }) {
            return builtIn.device
        }
        return list.first?.device
    }
    if let n = Int(spec) {
        return list.first { $0.index == n }?.device
    }
    let want = spec.lowercased()
    if let exact = list.first(where: { $0.name.lowercased() == want }) {
        return exact.device
    }
    return list.first(where: { $0.name.lowercased().contains(want) })?.device
}

func listCameras() {
    let list = videoDevices()
    for c in list {
        let fps = c.maxFPS >= 1 ? String(Int(c.maxFPS.rounded()))
                                : String(format: "%.1f", c.maxFPS)
        print("\(c.index)\t\(c.name)\t\(c.width)x\(c.height)\t\(fps)")
    }
    if list.isEmpty && permissionWord() == "notDetermined" {
        warn("No camera listed yet. macOS asks for camera permission the first "
             + "time KlipperCam looks through the lens — run "
             + "`KlipperCam --list` again after you click OK on that request.")
    }
}

func permissionWord() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    default: return "notDetermined"
    }
}

func permissionExit(_ word: String) -> Int32 {
    switch word {
    case "authorized": return 0
    case "denied": return 2
    case "restricted": return 3
    default: return 4
    }
}

func permissionHint() -> String {
    switch permissionWord() {
    case "denied":
        return "Camera access is switched off for KlipperCam. Open System "
             + "Settings, go to Privacy & Security > Camera, and turn KlipperCam on."
    case "restricted":
        return "This Mac does not allow KlipperCam to use the camera "
             + "(camera access is restricted by its settings)."
    default:
        return "KlipperCam did not get camera permission. Run it again and "
             + "click OK when macOS asks to use the camera."
    }
}

/// Calls `done` on the main queue. Never blocks the main queue: the permission
/// callback is delivered there, so blocking would deadlock the first run.
func ensureCameraAccess(_ done: @escaping (Bool) -> Void) {
    let status = AVCaptureDevice.authorizationStatus(for: .video)
    if status == .authorized {
        done(true)
        return
    }
    guard status == .notDetermined else {
        done(false)
        return
    }
    print("Waiting for camera permission — click OK in the macOS dialog.")
    AVCaptureDevice.requestAccess(for: .video) { granted in
        DispatchQueue.main.async { done(granted) }
    }
}

// The microphone is only ever asked about when the user records with --mic,
// so these stay separate from the camera helpers above: `--check-permission`
// keeps answering about the camera alone.

func micPermissionWord() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    default: return "notDetermined"
    }
}

func micPermissionHint() -> String {
    switch micPermissionWord() {
    case "denied":
        return "Microphone access is switched off for KlipperCam. Open System "
             + "Settings, go to Privacy & Security > Microphone, and turn "
             + "KlipperCam on — or run: tccutil reset Microphone "
             + "com.klipperformac.klippercam"
    case "restricted":
        return "This Mac does not allow KlipperCam to use the microphone "
             + "(microphone access is restricted by its settings)."
    default:
        return "KlipperCam did not get microphone permission. Run it again "
             + "and click OK when macOS asks to use the microphone."
    }
}

/// Calls `done` on the main queue; only ever reached from a --mic recording.
func ensureMicAccess(_ done: @escaping (Bool) -> Void) {
    let status = AVCaptureDevice.authorizationStatus(for: .audio)
    if status == .authorized {
        done(true)
        return
    }
    guard status == .notDetermined else {
        done(false)
        return
    }
    print("Waiting for microphone permission — click OK in the macOS dialog.")
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        DispatchQueue.main.async { done(granted) }
    }
}

func micDevices() -> [AVCaptureDevice] {
    // Same legacy listing as videoDevices(): on macOS 13 it is the one that
    // also sees USB and Continuity Camera microphones.
    return AVCaptureDevice.devices().filter { $0.hasMediaType(.audio) }
}

/// `--mic NAME` picks by name; empty means the microphone macOS would pick.
func selectMic(_ list: [AVCaptureDevice], _ spec: String) -> AVCaptureDevice? {
    if spec.isEmpty {
        return AVCaptureDevice.default(for: .audio) ?? list.first
    }
    let want = spec.lowercased()
    if let exact = list.first(where: { $0.localizedName.lowercased() == want }) {
        return exact
    }
    return list.first(where: { $0.localizedName.lowercased().contains(want) })
}

func listMics() {
    let list = micDevices()
    for (i, d) in list.enumerated() {
        print("\(i)\t\(d.localizedName)")
    }
    if list.isEmpty {
        warn("No microphone found on this Mac.")
    }
}
