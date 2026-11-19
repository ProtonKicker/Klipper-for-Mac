// Recording: while the dashboard gets its MJPEG pictures, the same capture
// session writes an .mp4 file next to them.
//
// Why AVCaptureMovieFileOutput and not AVAssetWriter: on the machine this was
// built on (macOS 13.7.8) AVAssetWriter's startSession(atSourceTime:) raises
// NSInternalInconsistencyException ("Cannot call method when status is 0")
// for every input combination — video only, audio only, .mov, nil properties —
// from both Swift and Objective-C, so no AVAssetWriter design can run there.
// AVCaptureMovieFileOutput does the same job inside the session and is the
// only AVFoundation writer proven working here. It costs a configurable
// bitrate (see --quality: it still drives the JPEG/picture path only).
//
// The rule that shapes all of this: the file on disk is either a complete MP4
// (finalized, playable) or it is not there at all. Every failure path — a
// signal, a recording error, a run that never started — removes the file
// instead of leaving a half-broken one behind.

import AVFoundation
import CoreMedia
import Foundation

final class Recorder: NSObject, AVCaptureFileOutputRecordingDelegate {
    struct Status {
        let state: String        // idle | recording | stopping | failed | done
        let seconds: Double      // media time AVFoundation has written
        let elapsed: Double      // wall clock since the file was opened
        let bytes: Int
        let wantsAudio: Bool
        let problem: String?

        /// One line for the loopback status endpoint, so the Python side can
        /// ask "is it really recording?" without parsing prose.
        var line: String {
            var out = "state=\(state) seconds=\(String(format: "%.1f", seconds)) "
                + "elapsed=\(String(format: "%.1f", elapsed)) "
                + "bytes=\(bytes) audio=\(wantsAudio ? "on" : "off")"
            if let problem = problem {
                out += " error=\(problem.replacingOccurrences(of: " ", with: "_"))"
            }
            return out
        }
    }

    let fileURL: URL
    private let wantsAudio: Bool

    private let lock = NSLock()
    /// AVFoundation sends the recording delegate's messages on the queue that
    /// started the recording, and closing the file waits for them. So every
    /// call that touches the file output happens here, never on the main
    /// queue — otherwise the close deadlocks against the callback that has to
    /// tell us it is done (measured, not theorised: the first version of this
    /// file hung forever when driven from the main queue).
    private let queue = DispatchQueue(label: "klippercam.record")
    private weak var output: AVCaptureFileOutput?
    private var state = "idle"
    private var startedAt: Date?
    private var problem: String?
    private var completion: ((URL?) -> Void)?

    init(path: String, wantsAudio: Bool) {
        self.fileURL = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        self.wantsAudio = wantsAudio
    }

    /// Makes the folder and clears an old file at the same path (mixing new
    /// bytes into an existing recording is worse than replacing it).
    /// Returns nil, or one plain sentence for the user.
    func prepare() -> String? {
        let folder = fileURL.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: folder,
                                                    withIntermediateDirectories: true)
        } catch {
            return "Could not create \(folder.path). Check the folder and try again."
        }
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: fileURL.path, isDirectory: &isDir) {
            if isDir.boolValue {
                return "\(fileURL.path) is a folder. Give a file name ending in .mp4."
            }
            do {
                try FileManager.default.removeItem(at: fileURL)
            } catch {
                return "Could not replace \(fileURL.path). Close it or pick another name."
            }
        }
        return nil
    }

    // MARK: - driving the session's file output

    /// Remembers the output this recorder writes through. Called while the
    /// session is being configured, before it runs.
    func attach(_ output: AVCaptureFileOutput) {
        lock.lock()
        self.output = output
        lock.unlock()
    }

    /// Starts the file. Call after the session is running, or AVFoundation
    /// quietly records nothing.
    func begin() {
        lock.lock()
        let output = self.output
        if state == "idle" { state = "starting"; startedAt = Date() }
        lock.unlock()
        guard let output = output else {
            note("the camera is not set up to record")
            return
        }
        queue.async {
            output.startRecording(to: self.fileURL, recordingDelegate: self)
        }
    }

    /// Closes the file. `done` gets the URL for a complete movie, or nil when
    /// there is nothing on disk worth keeping. It runs on the record queue when
    /// AVFoundation finishes writing — callers must never block the main queue
    /// waiting for it.
    func stop(_ done: @escaping (URL?) -> Void) {
        lock.lock()
        let output = self.output
        let wasRecording = state == "recording" || state == "starting"
        if wasRecording {
            state = "stopping"
            completion = done
        }
        lock.unlock()
        guard wasRecording else {
            // Never started (or already closed): there is nothing to finalize.
            discard()
            done(nil)
            return
        }
        guard let output = output else {
            finishWith(nil)
            return
        }
        // Returns at once: the answer comes in didFinishRecordingTo, on this
        // same queue, and `done` runs from there.
        queue.async {
            if output.isRecording {
                // This SDK's startRecording(to:recordingDelegate:) takes both
                // args as non-optional, so the documented nil-URL stop is not
                // expressible; stopRecording() is deprecated but is the one
                // that compiles here and does deliver didFinishRecordingTo.
                output.stopRecording()
            } else {
                // Nothing was ever handed to the writer, so there is no movie.
                self.finishWith(nil)
            }
        }
    }

    /// Used by the watchdog when the close never completes: no half file, no
    /// pending recording, nothing left to save.
    func abandon() {
        lock.lock()
        state = "failed"
        problem = "close-timed-out"
        completion = nil  // a late callback must not hand back a deleted file
        let output = self.output
        lock.unlock()
        if let output = output {
            queue.async {
                if output.isRecording { output.stopRecording() }  // best effort
            }
        }
        discard()
    }

    var status: Status {
        lock.lock()
        let output = self.output
        var s = state
        let openedAt = startedAt
        let p = problem
        let wants = wantsAudio
        lock.unlock()
        // macOS does not always deliver didStartRecordingTo, so the state a
        // watcher sees comes from the file output itself when it disagrees.
        if s == "starting", let output = output, output.isRecording { s = "recording" }
        var seconds = 0.0
        if let output = output {
            seconds = max(0, CMTimeGetSeconds(output.recordedDuration))
        }
        let elapsed = openedAt.map { max(0, Date().timeIntervalSince($0)) } ?? 0
        let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL.path)
        let bytes = (attrs?[.size] as? Int) ?? 0
        if s == "idle", bytes == 0 { seconds = 0 }
        return Status(state: s, seconds: seconds, elapsed: elapsed, bytes: bytes,
                      wantsAudio: wants, problem: p)
    }

    // MARK: - AVCaptureFileOutputRecordingDelegate

    func fileOutput(_ output: AVCaptureFileOutput,
                    didStartRecordingTo fileURL: URL,
                    from connections: [AVCaptureConnection]) {
        lock.lock()
        if state == "starting" || state == "idle" { state = "recording" }
        lock.unlock()
    }

    func fileOutput(_ output: AVCaptureFileOutput,
                    didFinishRecordingTo fileURL: URL,
                    from connections: [AVCaptureConnection],
                    error: Error?) {
        lock.lock()
        let abandoned = state == "failed"
        lock.unlock()
        if abandoned {
            // The watchdog already deleted this file; whatever arrives now is
            // whatever AVFoundation had left. It goes the same way.
            discard()
            return
        }
        if let error = error {
            // A failed finalize means the bytes are not a movie. Say so, and
            // delete rather than leave a file that plays back as garbage.
            note(error.localizedDescription)
            finishWith(nil)
            return
        }
        finishWith(fileURL)
    }

    // MARK: - internals

    private func note(_ message: String) {
        lock.lock()
        state = "failed"
        if problem == nil { problem = message }
        lock.unlock()
        warn("Recording to \(fileURL.path) did not work: \(message)")
    }

    private func finishWith(_ url: URL?) {
        var kept = url
        if let url = url {
            // A zero-byte "success" would be a broken file wearing a hat.
            let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
            if ((attrs?[.size] as? Int) ?? 0) == 0 { kept = nil }
        }
        lock.lock()
        let done = completion
        completion = nil
        if kept != nil {
            state = "done"
        } else if problem == nil {
            problem = "nothing-was-recorded"
            state = "failed"
        }
        lock.unlock()
        if kept == nil { discard() }
        done?(kept)
    }

    /// Never keeps a broken file: this is the one job it has.
    private func discard() {
        try? FileManager.default.removeItem(at: fileURL)
    }
}
