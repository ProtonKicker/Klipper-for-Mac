// The one frame of truth: the newest JPEG, plus whoever is waiting for the
// next one. Capture hands frames here; the HTTP server reads them.

import Foundation

final class Viewer {
    static let maxPending = 4

    private let lock = NSLock()
    private let signal = DispatchSemaphore(value: 0)
    private var pending: [Data] = []
    private var dead = false

    /// A viewer that cannot keep up is dropped instead of eating memory.
    func push(_ jpeg: Data) {
        lock.lock()
        if dead {
            lock.unlock()
            return
        }
        if pending.count >= Viewer.maxPending {
            dead = true
            pending.removeAll()
        } else {
            pending.append(jpeg)
        }
        lock.unlock()
        signal.signal()
    }

    func take() -> [Data] {
        lock.lock()
        defer { lock.unlock() }
        let out = pending
        pending.removeAll()
        return out
    }

    var dropped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return dead
    }

    func kill() {
        lock.lock()
        dead = true
        lock.unlock()
        signal.signal()
    }

    /// Wakes on the next frame, on a drop, or after `seconds`.
    func waitFrames(_ seconds: Double) {
        _ = signal.wait(timeout: .now() + seconds)
    }
}

final class FrameHub {
    private let lock = NSLock()
    private var viewers: [Viewer] = []
    private var latest: Data?
    private var closing = false

    func publish(_ jpeg: Data) {
        lock.lock()
        latest = jpeg
        let targets = viewers
        lock.unlock()
        for v in targets {
            v.push(jpeg)
        }
    }

    func attach() -> Viewer {
        let viewer = Viewer()
        lock.lock()
        viewers.append(viewer)
        lock.unlock()
        return viewer
    }

    func detach(_ viewer: Viewer) {
        lock.lock()
        viewers.removeAll { $0 === viewer }
        lock.unlock()
    }

    var shuttingDown: Bool {
        lock.lock()
        defer { lock.unlock() }
        return closing
    }

    func closeAll() {
        lock.lock()
        closing = true
        let targets = viewers
        viewers.removeAll()
        lock.unlock()
        for v in targets {
            v.kill()
        }
    }

    /// Newest frame, waiting up to `seconds` for the first one to arrive.
    func newest(wait seconds: Double) -> Data? {
        let deadline = Date().addingTimeInterval(seconds)
        while true {
            lock.lock()
            let jpeg = latest
            lock.unlock()
            if let jpeg = jpeg {
                return jpeg
            }
            if Date() >= deadline || closing {
                return nil
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
    }
}
