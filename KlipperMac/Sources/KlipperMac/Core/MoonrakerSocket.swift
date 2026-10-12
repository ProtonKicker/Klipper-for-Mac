import Foundation

enum SocketEvent {
    case connection(ready: Bool)     // transport + JSON-RPC handshake alive
    case klippy(KlippyState)
    case status(LivePrinterState)    // merged snapshot; consume on main actor only
    case gcodeResponse(String)
    case closed
}

/// WebSocket JSON-RPC client for Moonraker, mirroring the contract proven by
/// klipperformac/tests/selftest.py: ws://localhost:7125/websocket + Origin header.
/// Owns its own LivePrinterState (key-level diff merge) and emits merged copies.
final class MoonrakerSocket: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    /// Object -> keys to subscribe; every name is intersected with
    /// /printer/objects/list first, because one unknown object fails the call.
    static let desiredObjects: [String: [String]] = [
        "print_stats": ["filename", "state", "print_duration", "total_duration",
                        "progress", "message"],
        "virtual_sdcard": ["progress", "is_active", "file_position"],
        "display_status": ["progress", "message"],
        "extruder": ["temperature", "target", "power"],
        "heater_bed": ["temperature", "target", "power"],
        "toolhead": ["position", "homed_axes", "max_velocity"],
        "gcode_move": ["gcode_position", "speed_factor"],
        "fan": ["speed"],
        "configfile": ["settings", "save_config_pending"],
        "idle_timeout": ["state"],
        "motion_report": ["live_velocity"],
        "bed_mesh": ["status", "profile_name"],
    ]

    private let lock = NSLock()
    private var stream: AsyncStream<SocketEvent>!
    private var continuation: AsyncStream<SocketEvent>.Continuation?

    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private let delegateQueue: OperationQueue = {
        let q = OperationQueue()
        q.maxConcurrentOperationCount = 1
        return q
    }()

    private var desired = false
    private var port = 0
    private var origin = ""
    private var connectionId: Int?
    private var rpcId = 0
    private var pending: [Int: CheckedContinuation<JSONValue?, Never>] = [:]
    private var reconnectDelay: TimeInterval = 1
    private var state = LivePrinterState()

    override init() {
        super.init()
        let (s, c) = AsyncStream.makeStream(of: SocketEvent.self)
        stream = s
        continuation = c
    }

    func events() -> AsyncStream<SocketEvent> { stream }

    // MARK: - lifecycle

    func start(port: Int, origin: String) {
        lock.lock()
        if desired { lock.unlock(); return }
        self.port = port
        self.origin = origin
        desired = true
        lock.unlock()
        connect()
    }

    func stop() {
        lock.lock()
        desired = false
        let t = task
        task = nil
        let sess = session
        session = nil
        connectionId = nil
        let waiters = pending
        pending.removeAll()
        lock.unlock()
        for (_, c) in waiters { c.resume(returning: nil) }
        t?.cancel(with: .normalClosure, reason: nil)
        sess?.invalidateAndCancel()
        emit(.connection(ready: false))
    }

    private func connect() {
        lock.lock()
        let p = port
        let o = origin
        let alive = desired
        lock.unlock()
        guard alive, let url = URL(string: "ws://localhost:\(p)/websocket") else { return }

        var req = URLRequest(url: url)
        req.setValue(o, forHTTPHeaderField: "Origin")
        let sess = URLSession(configuration: .default, delegate: self, delegateQueue: delegateQueue)
        let t = sess.webSocketTask(with: req)
        lock.lock()
        session = sess
        task = t
        lock.unlock()
        t.resume()
        receiveNext()

        Task { await self.onTransportUp() }
    }

    private func onTransportUp() async {
        // Real klippy state straight from the server (works even when down).
        if let info = await Moonraker.getJSON("/server/info", port: port),
           let r = info["result"] as? [String: Any] {
            let connected = r["klippy_connected"] as? Bool ?? false
            let s = r["klippy_state"] as? String ?? "disconnected"
            let ks: KlippyState = !connected ? .disconnected
                : KlippyState(rawValue: s) ?? .unknown
            setKlippy(ks)
        }
        // Moonraker (current API) identifies native clients; the reply proves
        // the JSON-RPC round trip. Subscriptions bind to the socket itself.
        if let info = await rpc("server.connection.identify", params: [
            "client_name": "Klipper for Mac", "version": "1.0",
            "type": "desktop", "url": origin,
        ]), let cid = info["connection_id"]?.intValue {
            noteConnection(cid)
            emit(.connection(ready: true))
        }
        await beginSubscription(retriesLeft: 3, delay: 4)
    }

    /// objects/list errors while Klipper is down — retry a few times with
    /// backoff, after that rely on notify_klippy_ready to trigger a fresh one.
    private func beginSubscription(retriesLeft: Int, delay: TimeInterval) async {
        guard retriesLeft > 0 else { return }
        let objects = await Moonraker.objectList(port: port)
        if objects.isEmpty {
            await sleepSec(delay)
            await beginSubscription(retriesLeft: retriesLeft - 1, delay: delay * 2)
            return
        }
        var values: [String: Any] = [:]
        for (obj, keys) in Self.desiredObjects where objects.contains(obj) {
            values[obj] = keys
        }
        guard let result = await rpc("printer.objects.subscribe", params: ["objects": values]),
              let status = result["status"]?.objectValue, !status.isEmpty
        else {
            await sleepSec(delay)
            await beginSubscription(retriesLeft: retriesLeft - 1, delay: delay * 2)
            return
        }
        var snap: [String: [String: JSONValue]] = [:]
        for (o, v) in status { if let d = v.objectValue { snap[o] = d } }
        let copy = mergeSnapshot(snap)
        emit(.connection(ready: true))
        emit(.status(copy))
    }

    // MARK: - receive

    private func receiveNext() {
        lock.lock()
        let t = task
        lock.unlock()
        t?.receive { [weak self] result in
            switch result {
            case .success(let msg):
                self?.handle(msg)
                self?.receiveNext()
            case .failure:
                self?.transportDied()
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        let data: Data?
        switch message {
        case .string(let s): data = s.data(using: .utf8)
        case .data(let d): data = d
        @unknown default: data = nil
        }
        guard let data, let frame = try? JSONDecoder().decode(JSONValue.self, from: data),
              case .object(let obj) = frame
        else { return }

        if let method = obj["method"]?.stringValue {
            handleNotification(method, params: obj["params"])
            return
        }
        if let id = obj["id"]?.intValue {
            let cont: CheckedContinuation<JSONValue?, Never>?
            lock.lock()
            cont = pending.removeValue(forKey: id)
            lock.unlock()
            let result: JSONValue?
            if obj["error"] != nil { result = nil }
            else { result = obj["result"] }
            cont?.resume(returning: result)
        }
    }

    private func handleNotification(_ method: String, params: JSONValue?) {
        // Moonraker notifications send params as a positional array; be lenient.
        let first = params?.arrayValue?.first ?? params
        switch method {
        case "notify_status_update":
            guard let diffValue = first?.objectValue else { return }
            var diff: [String: [String: JSONValue]] = [:]
            for (o, v) in diffValue { if let d = v.objectValue { diff[o] = d } }
            lock.lock()
            state.applyStatusDiff(diff)
            let copy = state
            lock.unlock()
            emit(.status(copy))
        case "notify_gcode_response":
            let text = first?.stringValue
                ?? params?["result"]?.stringValue
            if let text { emit(.gcodeResponse(text)) }
        case "notify_klippy_ready":
            setKlippy(.ready)
            Task { await self.beginSubscription(retriesLeft: 2, delay: 4) }
        case "notify_klippy_shutdown":
            setKlippy(.shutdown)
        case "notify_klippy_disconnected":
            setKlippy(.disconnected)
        case "notify_klippy_connected":
            setKlippy(.startup)
        default:
            break
        }
    }

    private func setKlippy(_ ks: KlippyState) {
        lock.lock()
        let changed = state.klippy != ks
        state.klippy = ks
        let copy = state
        lock.unlock()
        if changed {
            emit(.klippy(ks))
            emit(.status(copy))
        }
    }

    // MARK: - send

    private func rpc(_ method: String, params: [String: Any]) async -> JSONValue? {
        guard let (t, id) = allocateRPC() else { return nil }
        let body: [String: Any] = ["jsonrpc": "2.0", "method": method,
                                   "params": params, "id": id]
        guard let data = try? JSONSerialization.data(withJSONObject: body),
              let text = String(data: data, encoding: .utf8) else {
            dropRPC(id)
            return nil
        }
        return await withCheckedContinuation { cont in
            registerRPC(id, cont)
            t.send(.string(text)) { [weak self] err in
                if err != nil {
                    self?.failRPC(id)
                }
            }
        }
    }

    private func failRPC(_ id: Int) {
        lock.lock()
        let cont = pending.removeValue(forKey: id)
        lock.unlock()
        cont?.resume(returning: nil)
    }

    // MARK: - reconnect

    private func transportDied() {
        lock.lock()
        // Idempotent: receive-failure and the delegate close path both fire for
        // one death; only the first gets to schedule a reconnect.
        guard desired, let t = task, let sess = session else {
            lock.unlock()
            return
        }
        task = nil
        session = nil
        connectionId = nil
        let waiters = pending
        pending.removeAll()
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, 30)
        lock.unlock()
        for (_, c) in waiters { c.resume(returning: nil) }
        _ = t
        sess.invalidateAndCancel()
        emit(.connection(ready: false))
        Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if self.stillDesired() { self.connect() }
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
                    reason: Data?) {
        lock.lock()
        let isCurrent = (webSocketTask === task)
        lock.unlock()
        if isCurrent { transportDied() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        lock.lock()
        // Only the *current* session's death triggers a reconnect — a previous
        // session finishing its invalidateAndCancel must not kill the new one.
        let isCurrent = (session === self.session)
        lock.unlock()
        if isCurrent { transportDied() }
    }

    // MARK: - plumbing (sync lock sites; async callers go through these)

    private func noteConnection(_ cid: Int) {
        lock.lock()
        connectionId = cid
        reconnectDelay = 1
        lock.unlock()
    }

    private func mergeSnapshot(_ snap: [String: [String: JSONValue]]) -> LivePrinterState {
        lock.lock()
        state.applySnapshot(snap)
        let copy = state
        lock.unlock()
        return copy
    }

    private func allocateRPC() -> (URLSessionWebSocketTask, Int)? {
        lock.lock()
        guard let t = task, desired else { lock.unlock(); return nil }
        rpcId += 1
        let id = rpcId
        lock.unlock()
        return (t, id)
    }

    private func registerRPC(_ id: Int, _ cont: CheckedContinuation<JSONValue?, Never>) {
        lock.lock()
        pending[id] = cont
        lock.unlock()
    }

    private func dropRPC(_ id: Int) {
        lock.lock()
        pending.removeValue(forKey: id)
        lock.unlock()
    }

    private func stillDesired() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return desired
    }

    private func emit(_ e: SocketEvent) {
        lock.lock()
        let c = continuation
        lock.unlock()
        c?.yield(e)
    }

    private func sleepSec(_ s: TimeInterval) async {
        try? await Task.sleep(nanoseconds: UInt64(s * 1_000_000_000))
    }
}
