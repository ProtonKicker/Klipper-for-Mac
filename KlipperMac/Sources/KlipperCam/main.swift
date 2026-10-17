// KlipperCam — puts this Mac's camera on the printer dashboard.
// Flags are parsed by hand on purpose: no dependencies, predictable errors.

import AVFoundation
import Foundation

let usage = """
KlipperCam — show your Mac camera inside your own printer dashboard.

  KlipperCam --stream [--port 8082] [--width 1280] [--height 720]
                     [--fps 15] [--quality 80] [--device NAME_OR_INT]
      Serve the camera on http://127.0.0.1:PORT/?action=stream (this Mac only).

  KlipperCam --record PATH [--seconds N] [--mic] [--mic NAME]
      The same stream, plus the whole print written to one .mp4 file at PATH.
      With --seconds N the file is closed and the helper stops by itself after
      N seconds; without it, stop with Ctrl-C (or SIGTERM) and the file is
      still complete. --mic adds the microphone to the recording; nothing ever
      listens to sound unless --mic is given, and sound lands in the file only,
      never in the stream. Ask a running helper how its recording is doing:
      http://127.0.0.1:PORT/?action=recordstatus
      A recording that cannot be finished is deleted, never left broken.

  KlipperCam --list
      Show the cameras you can pick with --device.

  KlipperCam --list-mics
      Show the microphones you can pick with --mic.

  KlipperCam --snapshot PATH [--timeout 10]
      Save one picture as a JPEG file, then stop.

  KlipperCam --check-permission
      Print authorized, denied, restricted or notDetermined for the camera.

  KlipperCam --check-mic-permission
      The same words for the microphone. Asks for nothing, prompts for nothing.

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
    var record = ""
    var seconds = 0.0
    /// nil = sound was never asked for. "" = the default microphone.
    /// Anything else = a name from --list-mics.
    var mic: String?
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
        case "--list-mics": o.action = "list-mics"
        case "--snapshot":
            o.action = "snapshot"
            o.path = take("--snapshot")
        case "--check-permission": o.action = "check-permission"
        case "--check-mic-permission": o.action = "check-mic-permission"
        case "--help", "-h": o.action = "help"
        case "--port": o.port = number("--port")
        case "--width": o.width = number("--width")
        case "--height": o.height = number("--height")
        case "--fps": o.fps = number("--fps")
        case "--quality": o.quality = number("--quality")
        case "--device": o.device = take("--device")
        case "--record": o.record = take("--record")
        case "--seconds":
            let value = take("--seconds")
            guard let seconds = Double(value), seconds > 0 else {
                die("--seconds needs a length like 30, got \(value).")
            }
            o.seconds = seconds
        case "--mic":
            // --mic, --mic on, --mic off and --mic NAME all mean what the
            // Python side writes; a bare --mic takes the default microphone.
            if i + 1 < argv.count, !argv[i + 1].hasPrefix("-") {
                let value = argv[i + 1]
                i += 1
                switch value.lowercased() {
                case "on", "yes", "true", "1": o.mic = ""
                case "off", "no", "false", "0": o.mic = nil
                default: o.mic = value
                }
            } else {
                o.mic = ""
            }
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
    if o.seconds > 0, o.record.isEmpty {
        die("--seconds only counts down a recording; give it a --record PATH.")
    }
    if o.mic != nil, o.record.isEmpty {
        die("--mic records sound into a video; give it a --record PATH. "
            + "(The live stream carries no sound at all.)")
    }
    if !o.record.isEmpty, o.action == "snapshot" {
        die("--record writes a video; --snapshot writes one picture. Pick one.")
    }
    return o
}

// Retained here so the camera outlives the permission callback.
var activeCamera: Camera?
var signalSources: [DispatchSourceSignal] = []
/// One stop only, however many signals or timers arrive: the file is closed
/// exactly once.
var stopping = false

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

func fileSize(_ url: URL) -> Int {
    let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
    return (attrs?[.size] as? Int) ?? 0
}

/// One serial queue for the whole shutdown: the AVFoundation file output
/// finishes a movie by sending itself a message on the MAIN thread and
/// waiting for it (`graphWillStop` -> performSelectorOnMainThread
/// waitUntilDone:YES). Called from inside a main-queue block that deadlocks
/// forever, which is exactly what the first version of this did — measured,
/// not guessed. So no part of the stop path ever runs on the main queue.
let stopQueue = DispatchQueue(label: "klippercam.stop")

func requestStop(_ server: MjpegServer, _ recorder: Recorder?) {
    stopQueue.async { stopEverything(server, recorder) }
}

/// Stops recording, waits for AVFoundation to close the file, then hands the
/// camera back and exits. Never returns; the process stays alive only until
/// the movie is complete or known to be worthless.
func stopEverything(_ server: MjpegServer, _ recorder: Recorder?) {
    if stopping { return }
    stopping = true
    server.stop()  // viewers first: they must not see a half-file's last frame
    guard let recorder = recorder else {
        activeCamera?.stop()
        _exit(0)
    }
    // A close that never completes must still leave no broken file behind.
    DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
        warn("The recording did not close within 20 seconds. Removing the "
             + "unfinished file instead of leaving it broken.")
        recorder.abandon()
        // _exit, not exit: a hung AVFoundation worker thread must never be
        // able to keep the process (and the lens) alive after this point.
        _exit(3)
    }
    recorder.stop { url in
        activeCamera?.stop()  // hand the lens back once the file is settled
        guard let url = url else {
            warn("Nothing was recorded: the camera sent no picture, "
                 + "so no file was left behind.")
            _exit(3)
        }
        let s = recorder.status
        print("Recorded \(url.path) (\(String(format: "%.1f", s.seconds)) s, "
              + "\(s.bytes) bytes, sound \(s.wantsAudio ? "on" : "off"))")
        _exit(0)
    }
}

func runStream(_ o: Options) {
    let device = chosenDevice(o.device)
    var recorder: Recorder?
    if !o.record.isEmpty {
        let made = Recorder(path: o.record, wantsAudio: o.mic != nil)
        if let problem = made.prepare() { die(problem) }
        recorder = made
    }
    // Bind the port first: the dashboard then has somewhere to knock even
    // while macOS is still asking the user about camera access.
    let hub = FrameHub()
    let server = MjpegServer(hub: hub, port: o.port)
    server.setRecorder(recorder)
    watchStopSignals { requestStop(server, recorder) }
    if let problem = server.start() { die(problem) }
    print("KlipperCam is serving \(device.localizedName) on "
          + "http://127.0.0.1:\(o.port)/?action=stream")
    if let recorder = recorder {
        print("Recording to \(recorder.fileURL.path)"
              + (o.mic == nil ? " (picture only)" : " with sound"))
    }
    if o.seconds > 0 {
        print("Closing the recording on its own in "
              + String(format: "%.0f", o.seconds) + " seconds.")
    }

    func startCamera(withMic mic: AVCaptureDevice?) {
        let camera = Camera(device: device, width: o.width, height: o.height,
                            fps: o.fps, quality: o.quality, hub: hub, mic: mic)
        camera.recorder = recorder
        if let problem = camera.start() { die(problem) }
        activeCamera = camera
        recorder?.begin()  // only once the session is actually running
        print("Camera ready: \(device.localizedName) \(o.width)x\(o.height) "
              + "at \(o.fps) fps. Leave this running; press Ctrl-C to stop.")
        if let mic = mic {
            print("Microphone ready: \(mic.localizedName). Sound goes into the "
                  + "recording only, never into the stream.")
        }
        if o.seconds > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + o.seconds) {
                requestStop(server, recorder)
            }
        }
    }

    ensureCameraAccess { granted in
        guard granted else { die(permissionHint()) }
        guard let spec = o.mic else {
            startCamera(withMic: nil)
            return
        }
        // macOS only asks about the microphone when --mic was actually used.
        var micDecided = false
        let micWait = 30.0
        // A permission dialog nobody answers must not strand the recording:
        // after a while the picture is all we can honestly deliver.
        DispatchQueue.main.asyncAfter(deadline: .now() + micWait) {
            guard !micDecided else { return }
            micDecided = true
            warn("The microphone permission question went unanswered for "
                 + String(Int(micWait)) + " seconds. Recording the picture only.")
            startCamera(withMic: nil)
        }
        ensureMicAccess { allowed in
            guard !micDecided else { return }
            micDecided = true
            guard allowed else {
                warn("No sound: \(micPermissionHint()) Recording the picture only.")
                startCamera(withMic: nil)
                return
            }
            guard let mic = selectMic(micDevices(), spec) else {
                die(spec.isEmpty
                    ? "This Mac has no microphone KlipperCam can record. "
                      + "Run: KlipperCam --list-mics"
                    : "No microphone called \(spec). Run: KlipperCam --list-mics")
            }
            startCamera(withMic: mic)
        }
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
case "check-mic-permission":
    let word = micPermissionWord()
    print(word)
    exit(permissionExit(word))
case "list":
    listCameras()
    exit(0)
case "list-mics":
    listMics()
    exit(0)
case "snapshot":
    runSnapshot(options)
default:
    runStream(options)
}
// AVFoundation's file output delivers its recording callbacks through the main
// RUN LOOP (its internals use performSelectorOnMainThread:waitUntilDone:), and
// dispatchMain() alone drains the main queue without ever pumping a run loop —
// the recording then waits forever for a close that cannot arrive. Every exit
// path above calls _exit, so this loop never needs to end by itself.
while true {
    RunLoop.main.run(mode: .default, before: Date.distantFuture)
}
