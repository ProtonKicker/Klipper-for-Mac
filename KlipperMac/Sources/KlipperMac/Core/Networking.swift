import Foundation
import Darwin

enum Net {
    /// Primary LAN IPv4 via a UDP "connect" (no packets are sent) — same trick
    /// as detect.lan_ip(): the kernel picks the routed interface for us.
    static func lanIP() -> String? {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        if fd < 0 { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(1).bigEndian
        inet_pton(AF_INET, "10.255.255.255", &addr.sin_addr)
        let connected = withUnsafePointer(to: &addr) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if connected != 0 { return nil }
        var bound = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let ok = withUnsafeMutablePointer(to: &bound) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &len)
            }
        }
        if ok != 0 { return nil }
        var buf = [CChar](repeating: 0, count: 64)
        inet_ntop(AF_INET, &bound.sin_addr, &buf, socklen_t(buf.count))
        return String(cString: buf)
    }
}
