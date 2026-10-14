// A deliberately small HTTP server on 127.0.0.1 only. BSD sockets + a blocking
// accept loop on its own queue: nothing else in this helper needs more, and a
// viewer that goes away must never take the camera down with it.

import Darwin
import Foundation

final class MjpegServer {
    static let boundary = "klipperformac"

    private let hub: FrameHub
    private let acceptQueue = DispatchQueue(label: "klippercam.accept")
    private let clientQueue = DispatchQueue(label: "klippercam.clients",
                                            attributes: .concurrent)
    private let lock = NSLock()
    private var listenFD: Int32 = -1
    private var running = false
    private var live = 0
    private let maxLive = 8
    private let port: Int

    init(hub: FrameHub, port: Int) {
        self.hub = hub
        self.port = port
    }

    /// Binds and starts serving. Returns nil, or one plain sentence.
    func start() -> String? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return "Could not open a network port on this Mac." }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(port).bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else {
            close(fd)
            return "Port \(port) is busy. Close the other camera app or pick "
                 + "--port \(port + 1)."
        }
        guard listen(fd, 8) == 0 else {
            close(fd)
            return "Port \(port) is busy. Try another --port."
        }
        lock.lock()
        listenFD = fd
        running = true
        lock.unlock()
        acceptQueue.async { self.acceptLoop(fd) }
        return nil
    }

    func stop() {
        lock.lock()
        let fd = listenFD
        running = false
        listenFD = -1
        lock.unlock()
        if fd >= 0 {
            shutdown(fd, SHUT_RDWR)
            close(fd)
        }
        hub.closeAll()
    }

    private func acceptLoop(_ serverFD: Int32) {
        while true {
            lock.lock()
            let keepGoing = running
            lock.unlock()
            if !keepGoing { return }
            var peer = sockaddr_in()
            var len = socklen_t(MemoryLayout<sockaddr_in>.size)
            let fd = withUnsafeMutablePointer(to: &peer) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    accept(serverFD, $0, &len)
                }
            }
            if fd < 0 {
                if errno == EINTR { continue }
                usleep(50_000)  // listener closing, or a momentary shortage
                continue
            }
            // SO_NOSIGPIPE: a viewer that hangs up must not kill the camera.
            var yes: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout<Int32>.size))
            setSocketTimeout(fd, SO_SNDTIMEO, 5)
            setSocketTimeout(fd, SO_RCVTIMEO, 10)
            lock.lock()
            if live >= maxLive {
                lock.unlock()
                sendText(fd, 503, "Too many viewers. Close one of them and reload.")
                close(fd)
                continue
            }
            live += 1
            lock.unlock()
            clientQueue.async {
                self.serve(fd)
                self.lock.lock()
                self.live -= 1
                self.lock.unlock()
                close(fd)
            }
        }
    }

    private func setSocketTimeout(_ fd: Int32, _ option: Int32, _ seconds: Int) {
        var tv = timeval(tv_sec: seconds, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, option, &tv, socklen_t(MemoryLayout<timeval>.size))
    }

    private func serve(_ fd: Int32) {
        guard let request = readRequest(fd), let head = request.first else { return }
        let fields = head.split(separator: " ")
        guard fields.count >= 2, String(fields[0]).uppercased() == "GET" else {
            sendText(fd, 404, "Not found")
            return
        }
        let (path, query) = splitTarget(String(fields[1]))
        let lower = query.lowercased()
        if lower.contains("action=stream") {
            stream(fd)
        } else if lower.contains("action=snapshot") || path == "/snapshot" {
            snapshot(fd)
        } else if path == "/healthz" {
            sendText(fd, 200, "ok")
        } else {
            sendText(fd, 404, "Not found")
        }
    }

    private func splitTarget(_ target: String) -> (String, String) {
        guard let mark = target.firstIndex(of: "?") else { return (target, "") }
        return (String(target[..<mark]), String(target[target.index(after: mark)...]))
    }

    private func stream(_ fd: Int32) {
        let headers = "HTTP/1.1 200 OK\r\n"
            + "Content-Type: multipart/x-mixed-replace;boundary=\(MjpegServer.boundary)\r\n"
            + "Multipart-boundary: \(MjpegServer.boundary)\r\n"
            + "Cache-Control: no-store, no-cache, must-revalidate\r\n"
            + "Access-Control-Allow-Origin: *\r\n"
            + "Connection: close\r\n\r\n"
        guard writeAll(fd, Data(headers.utf8)) else { return }
        let viewer = hub.attach()
        defer { hub.detach(viewer) }
        while !viewer.dropped && !hub.shuttingDown {
            viewer.waitFrames(0.5)
            for jpeg in viewer.take() {
                var part = Data("--\(MjpegServer.boundary)\r\n".utf8)
                part.append(Data("Content-Type: image/jpeg\r\n".utf8))
                part.append(Data("Content-Length: \(jpeg.count)\r\n\r\n".utf8))
                part.append(jpeg)
                part.append(Data("\r\n".utf8))
                if !writeAll(fd, part) { return }  // viewer left; that is normal
            }
        }
    }

    private func snapshot(_ fd: Int32) {
        guard let jpeg = hub.newest(wait: 2.0) else {
            sendText(fd, 503, "The camera has not sent a picture yet. Try again.")
            return
        }
        let headers = "HTTP/1.1 200 OK\r\n"
            + "Content-Type: image/jpeg\r\n"
            + "Content-Length: \(jpeg.count)\r\n"
            + "Cache-Control: no-store\r\n"
            + "Access-Control-Allow-Origin: *\r\n"
            + "Connection: close\r\n\r\n"
        var out = Data(headers.utf8)
        out.append(jpeg)
        _ = writeAll(fd, out)
    }

    private func sendText(_ fd: Int32, _ code: Int, _ message: String) {
        let reason = code == 200 ? "OK" : (code == 404 ? "Not Found" : "Service Unavailable")
        let body = Data(message.utf8)
        let head = "HTTP/1.1 \(code) \(reason)\r\n"
            + "Content-Type: text/plain; charset=utf-8\r\n"
            + "Content-Length: \(body.count)\r\n"
            + "Connection: close\r\n\r\n"
        var out = Data(head.utf8)
        out.append(body)
        _ = writeAll(fd, out)
    }

    private func readRequest(_ fd: Int32) -> [String]? {
        var buffer = [UInt8](repeating: 0, count: 2048)
        var text = ""
        while text.utf8.count < 8192 {
            let got = recv(fd, &buffer, buffer.count, 0)
            if got > 0 {
                text += String(decoding: buffer[0..<got], as: UTF8.self)
                if text.contains("\r\n\r\n") || text.contains("\n\n") { break }
                continue
            }
            if got < 0 && errno == EINTR { continue }
            break  // hung up, or too slow to send a request
        }
        guard !text.isEmpty else { return nil }
        return text.components(separatedBy: "\r\n")
    }

    private func writeAll(_ fd: Int32, _ data: Data) -> Bool {
        if data.isEmpty { return true }
        return data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Bool in
            guard let start = raw.baseAddress else { return false }
            var sent = 0
            while sent < raw.count {
                let n = send(fd, start + sent, raw.count - sent, 0)
                if n > 0 {
                    sent += n
                    continue
                }
                if n < 0 && errno == EINTR { continue }
                return false  // EPIPE, reset, or a viewer that stopped reading
            }
            return true
        }
    }
}
