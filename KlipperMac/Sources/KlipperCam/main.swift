// KlipperCam — puts this Mac's camera on the printer dashboard.
// Flags are parsed by hand on purpose: no dependencies, predictable errors.

import AVFoundation
import Foundation

let usage = """
KlipperCam — show your Mac camera inside your own printer dashboard.

  KlipperCam --stream [--port 8082] [--width 1280] [--height 720]
                     [--fps 15] [--quality 80] [--device NAME_OR_INT]
      Serve the camera on http://127.0.0.1:PORT/?action=stream (this Mac only).

  KlipperCam --list
      Show the cameras you can pick with --device.

  KlipperCam --snapshot PATH [--timeout 10]
      Save one picture as a JPEG file, then stop.

  KlipperCam --check-permission
      Print authorized, denied, restricted or notDetermined.

  KlipperCam --help

Nothing here reaches another computer. In Mainsail or Fluidd use
http://127.0.0.1:8082/?action=stream
"""

struct Options {
    var action = "stream"
    var port = 8082
    var width = 1280
    var height = 720
    var fps = 15
    var quality = 80
    var device = ""
    var path = ""
    var timeout = 10.0
}

func warn(_ message: String) {
    FileHandle.standardError.write(Data("KlipperCam: \(message)\n".utf8))
}

func die(_ message: String) -> Never {
    warn(message)
    exit(1)
}

func parseArgs(_ argv: [String]) -> Options {
    var o = Options()
    var i = 0

    func take(_ flag: String) -> String {
        guard i + 1 < argv.count else { die("\(flag) needs a value. See --help.") }
        let value = argv[i + 1]
        guard !value.hasPrefix("-") else { die("\(flag) needs a value. See --help.") }
        i += 1
        return value
    }

    func number(_ flag: String) -> Int {
        let value = take(flag)
        guard let n = Int(value) else { die("\(flag) needs a whole number, got \(value).") }
        return n
    }

    while i < argv.count {
        switch argv[i] {
        case "--stream": o.action = "stream"
        case "--list": o.action = "list"
        case "--snapshot":
            o.action = "snapshot"
            o.path = take("--snapshot")
        case "--check-permission": o.action = "check-permission"
        case "--help", "-h": o.action = "help"
        case "--port": o.port = number("--port")
        case "--width": o.width = number("--width")
        case "--height": o.height = number("--height")
        case "--fps": o.fps = number("--fps")
        case "--quality": o.quality = number("--quality")
        case "--device": o.device = take("--device")
        case "--timeout":
            let value = take("--timeout")
            guard let seconds = Double(value), seconds > 0 else {
                die("--timeout needs seconds like 10, got \(value).")
            }
            o.timeout = seconds
        default: die("I don't know the option \(argv[i]). Run: KlipperCam --help")
        }
        i += 1
    }
    guard o.port > 1 && o.port < 65535 else { die("--port must be between 2 and 65534.") }
    guard o.width > 0 && o.height > 0 else { die("--width and --height must be bigger than 0.") }
    guard o.fps > 0 && o.fps <= 60 else { die("--fps must be between 1 and 60.") }
    guard o.quality >= 1 && o.quality <= 100 else { die("--quality must be between 1 and 100.") }
    return o
}

// Retained here so the camera outlives the permission callback.
var activeCamera: Camera?
var signalSources: [DispatchSourceSignal] = []

/// Fails fast: a typo in --device should not wait behind a permission dialog.
func chosenDevice(_ spec: String) -> AVCaptureDevice {
    let list = videoDevices()
    if let device = selectDevice(list, spec) {
        return device
    }
    if list.isEmpty {
        die("No camera found on this Mac. Connect one, or use an iPhone with "
            + "Continuity Camera. If macOS has not asked you about camera "
            + "access yet, allow it in System Settings > Privacy & Security > "
            + "Camera.")
    }
    if Int(spec) != nil {
        die("There is no camera number \(spec). Run: KlipperCam --list")
    }
    die("No camera called \(spec). Run: KlipperCam --list")
}

func watchStopSignals(_ onStop: @escaping () -> Void) {
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { onStop() }
        source.resume()
        signalSources.append(source)
    }
}

func runStream(_ o: Options) {
    let device = chosenDevice(o.device)
    // Bind the port first: the dashboard then has somewhere to knock even
    // while macOS is still asking the user about camera access.
    let hub = FrameHub()
    let server = MjpegServer(hub: hub, port: o.port)
    watchStopSignals {
        activeCamera?.stop()
        server.stop()
        exit(0)
    }
    if let problem = server.start() { die(problem) }
    print("KlipperCam is serving \(device.localizedName) on "
          + "http://127.0.0.1:\(o.port)/?action=stream")
    ensureCameraAccess { granted in
        guard granted else { die(permissionHint()) }
        let camera = Camera(device: device, width: o.width, height: o.height,
                            fps: o.fps, quality: o.quality, hub: hub)
        if let problem = camera.start() { die(problem) }
        activeCamera = camera
        print("Camera ready: \(device.localizedName) \(o.width)x\(o.height) "
              + "at \(o.fps) fps. Leave this running; press Ctrl-C to stop.")
    }
}

func runSnapshot(_ o: Options) {
    let budget = o.timeout
    let device = chosenDevice(o.device)
    DispatchQueue.main.asyncAfter(deadline: .now() + budget + 20) {
        die("Timed out waiting for camera permission. Click OK when macOS asks "
            + "to use the camera, then try again.")
    }
    ensureCameraAccess { granted in
        guard granted else { die(permissionHint()) }
        let hub = FrameHub()
        let camera = Camera(device: device, width: o.width, height: o.height,
                            fps: max(o.fps, 5), quality: o.quality, hub: hub)
        if let problem = camera.start() { die(problem) }
        guard let jpeg = hub.newest(wait: budget) else {
            camera.stop()
            die("No picture from \(device.localizedName) within "
                + String(Int(budget)) + " seconds. Close other apps using the "
                + "camera, then try again.")
        }
        camera.stop()  // hand the lens back before touching the disk
        let url = URL(fileURLWithPath: (o.path as NSString).expandingTildeInPath)
        let folder = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: folder,
                                                 withIntermediateDirectories: true)
        do {
            try jpeg.write(to: url)
        } catch {
            die("Could not save \(url.path). Check the folder and try again.")
        }
        print("Saved \(url.path)")
        exit(0)
    }
}

// The dashboard reads this helper's log while it runs, so keep stdout unbuffered.
setvbuf(stdout, nil, _IONBF, 0)

let options = parseArgs(Array(CommandLine.arguments.dropFirst()))
switch options.action {
case "help":
    print(usage)
    exit(0)
case "check-permission":
    let word = permissionWord()
    print(word)
    exit(permissionExit(word))
case "list":
    listCameras()
    exit(0)
case "snapshot":
    runSnapshot(options)
default:
    runStream(options)
}
dispatchMain()
