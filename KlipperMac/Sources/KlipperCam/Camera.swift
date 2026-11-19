// AVCaptureSession -> BGRA frames -> JPEG bytes, paced to the requested fps.
// When this run also records, the same session writes the .mp4 next to the
// stream: macOS allows one AVCaptureSession per camera, so recording has to be
// a capability of the helper that already owns the lens.

import AVFoundation
import CoreMedia
import ImageIO
import Foundation

final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let session = AVCaptureSession()
    private let capture = AVCaptureVideoDataOutput()
    private let queue = DispatchQueue(label: "klippercam.capture")
    private let hub: FrameHub
    private let device: AVCaptureDevice
    private let mic: AVCaptureDevice?
    private let width: Int
    private let height: Int
    private let fps: Int
    private let quality: Double
    private let gap: Double
    private var lastPublish = 0.0  // only read/written on `queue`
    private var errorWatch: NSObjectProtocol?

    /// Set before `start()`: it gets a file output of its own inside this
    /// session, so the picture on the dashboard and the file on disk come out
    /// of one camera and one configuration.
    var recorder: Recorder?

    init(device: AVCaptureDevice, width: Int, height: Int, fps: Int,
         quality: Int, hub: FrameHub, mic: AVCaptureDevice? = nil) {
        self.device = device
        self.mic = mic
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = Double(quality) / 100.0
        self.gap = 1.0 / Double(fps)
        self.hub = hub
        super.init()
        errorWatch = NotificationCenter.default.addObserver(
            forName: .AVCaptureSessionRuntimeError, object: session, queue: nil)
            { _ in
                warn("The camera stopped sending frames. Close any other app "
                     + "using it, then start KlipperCam again.")
            }
    }

    /// Starts the session. Returns nil, or one plain sentence for the user.
    func start() -> String? {
        var problem: String?
        queue.sync { problem = openAndRun() }
        return problem
    }

    func stop() {
        if let watch = errorWatch {
            NotificationCenter.default.removeObserver(watch)
            errorWatch = nil
        }
        queue.sync { session.stopRunning() }
        hub.closeAll()
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sample: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        let now = CFAbsoluteTimeGetCurrent()
        if now - lastPublish < gap {
            return  // capture may run faster than the stream should
        }
        lastPublish = now
        guard let jpeg = Camera.jpeg(from: sample, quality: quality) else { return }
        hub.publish(jpeg)
    }

    private func openAndRun() -> String? {
        guard let input = try? AVCaptureDeviceInput(device: device) else {
            return "Could not open \(device.localizedName). Another app is "
                 + "probably using it — quit that app and try again."
        }
        var micInput: AVCaptureDeviceInput?
        if let mic = mic {
            do {
                micInput = try AVCaptureDeviceInput(device: mic)
            } catch {
                return "Could not open \(mic.localizedName) for sound. "
                     + "Another app is probably using it — quit that app, "
                     + "or run without --mic."
            }
        }
        session.beginConfiguration()
        session.sessionPreset = .high
        if session.canAddInput(input) {
            session.addInput(input)
        }
        // The microphone goes in here, in the same configuration, because
        // AVFoundation refuses new inputs on a session that is already
        // running — and a mid-print reconfigure is exactly what must not
        // happen. It is only ever added for a recording: the stream has no
        // sound at all.
        if let micInput = micInput {
            if session.canAddInput(micInput) {
                session.addInput(micInput)
            } else {
                return "This Mac will not add \(micInput.device.localizedName) "
                     + "to the camera session. Run without --mic, or pick "
                     + "another microphone with --mic NAME."
            }
        }
        capture.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        capture.alwaysDiscardsLateVideoFrames = true
        capture.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(capture) {
            session.addOutput(capture)
        }
        if let recorder = recorder {
            let movie = AVCaptureMovieFileOutput()
            if session.canAddOutput(movie) {
                session.addOutput(movie)
                recorder.attach(movie)
            } else {
                return "This Mac will not add a file recorder to the camera "
                     + "session, so nothing can be recorded. The picture "
                     + "stream itself still works without --record."
            }
        }
        session.commitConfiguration()
        if session.inputs.isEmpty || session.outputs.isEmpty {
            return "Could not get picture from \(device.localizedName). "
                 + "Try another camera (--list shows what you have)."
        }
        pickFormat()
        session.startRunning()
        return nil
    }

    /// Runs with the session configured but not yet streaming, which is when
    /// AVFoundation accepts a mode change.
    private func pickFormat() {
        let want = Camera.matchFormat(device, width: width, height: height, fps: fps)
        let dims = want.formatDescription.dimensions
        if Int(dims.width) != width || Int(dims.height) != height {
            warn("This camera has no \(width)x\(height) mode, using "
                 + "\(dims.width)x\(dims.height) instead.")
        }
        do {
            try device.lockForConfiguration()
        } catch {
            return
        }
        defer { device.unlockForConfiguration() }
        device.activeFormat = want
        if Camera.supports(want, fps: fps) {
            let rate = CMTime(value: 1, timescale: CMTimeScale(fps))
            device.activeVideoMinFrameDuration = rate
            device.activeVideoMaxFrameDuration = rate
        }
    }

    private static func matchFormat(_ device: AVCaptureDevice, width: Int,
                                    height: Int,
                                    fps: Int) -> AVCaptureDevice.Format {
        var wanted: AVCaptureDevice.Format?
        for f in device.formats {
            let d = f.formatDescription.dimensions
            guard Int(d.width) == width && Int(d.height) == height else { continue }
            if supports(f, fps: fps) {
                return f
            }
            wanted = f
        }
        return wanted ?? topFormat(device)
    }

    private static func supports(_ format: AVCaptureDevice.Format, fps: Int) -> Bool {
        for r in format.videoSupportedFrameRateRanges where r.maxFrameRate >= Double(fps) {
            return true
        }
        return false
    }

    private static func jpeg(from sample: CMSampleBuffer, quality: Double) -> Data? {
        guard let buffer = CMSampleBufferGetImageBuffer(sample) else { return nil }
        // The pixel memory belongs to the camera; encoding happens while it is
        // locked so nothing underneath can change mid-JPEG.
        guard CVPixelBufferLockBaseAddress(buffer, []) == kCVReturnSuccess else {
            return nil
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue
                                 | CGBitmapInfo.byteOrder32Little.rawValue)
        guard let context = CGContext(data: base,
                                      width: CVPixelBufferGetWidth(buffer),
                                      height: CVPixelBufferGetHeight(buffer),
                                      bitsPerComponent: 8,
                                      bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: info.rawValue),
              let image = context.makeImage() else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString,
                                                         1, nil) else { return nil }
        let options: CFDictionary = [kCGImageDestinationLossyCompressionQuality as String:
                                        quality] as CFDictionary
        CGImageDestinationAddImage(dest, image, options)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
    }
}
