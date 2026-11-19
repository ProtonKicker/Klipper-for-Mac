import SwiftUI
import WebKit

struct CameraView: View {
    @EnvironmentObject private var model: AppModel

    @State private var devices: [String] = []
    @State private var devicesHint = ""
    @State private var devicesLoaded = false
    @State private var prefsLoaded = false
    @State private var triedStarting = false

    @State private var selectedDevice = ""
    @State private var savedDevice = ""
    @State private var fps = 15
    @State private var savedFps = 15
    @State private var resolution = "1280 x 720"
    @State private var savedResolution = "1280 x 720"

    // --- recording modes ---
    // captureMedium: what sensor data is captured. audio is audio-only (no
    // frames); video and audioVideo also take video frames.
    @State private var medium: CaptureMedium = .audioVideo
    // recordingStyle: bounded clip ("Videos") or unbounded interval snapshot
    // ("Timelapse"). Timelapse has no audio and uses larger frame intervals.
    @State private var style: RecordingStyle = .videos
    @State private var timelapseInterval: TimelapseInterval = .sec15

    private enum CaptureMedium: String, CaseIterable, Identifiable {
        case audio, video, audioVideo
        var id: String { rawValue }
        var label: String {
            switch self {
            case .audio: return "Audio only"
            case .video: return "Video only"
            case .audioVideo: return "Audio + Video"
            }
        }
        var icon: String {
            switch self {
            case .audio: return "waveform"
            case .video: return "video"
            case .audioVideo: return "waveform.circle.fill"
            }
        }
        var hasAudio: Bool { self != .video }
    }

    private enum RecordingStyle: String, CaseIterable, Identifiable {
        case videos, timelapse
        var id: String { rawValue }
        var label: String {
            switch self {
            case .videos: return "Videos"
            case .timelapse: return "Timelapse"
            }
        }
    }

    private enum TimelapseInterval: Int, CaseIterable, Identifiable {
        case sec15 = 15, min1 = 60, min5 = 300, min10 = 600
        var id: Int { rawValue }
        var label: String {
            switch self {
            case .sec15: return "15 sec"
            case .min1: return "1 min"
            case .min5: return "5 min"
            case .min10: return "10 min"
            }
        }
        /// Frame interval in seconds.
        var seconds: Int { rawValue }
    }

    private let fpsOptions = [10, 15, 20, 25, 30]
    private let resolutionOptions: [(label: String, width: Int, height: Int)] = [
        ("640 x 480", 640, 480),
        ("1280 x 720", 1280, 720),
        ("1920 x 1080", 1920, 1080),
    ]

    private var env: KEnvironment { model.env }
    private var camOn: Bool { model.camOnline == true }
    private var streamURL: String { "\(env.camURL)/?action=stream" }
    private var statePresentation: CameraStatePresentation {
        CameraFeedback.statePresentation(isOnline: model.camOnline)
    }

    var body: some View {
        Form {
            cameraSection
            recordingSection
            previewSection
            snapshotSection
            settingsSection
            privacySection
        }
        .formStyle(.grouped)
        .navigationTitle("Recording")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await reloadCamera() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
            }
        }
        .onAppear {
            loadPrefs()
            loadDevices()
        }
    }

    // MARK: - what this is, and the one button

    private var cameraSection: some View {
        Section("Camera") {
            Text("Watch your printer while it works — use the camera built into this Mac, or an iPhone next to the printer.")
                .font(.callout)
                .foregroundStyle(.secondary)

            HStack(spacing: 6) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 8, height: 8)
                Text(statePresentation.text)
                    .font(.callout.weight(.medium))
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
            }

            HStack(spacing: 8) {
                Button {
                    // Only a start attempt can mean "macOS is waiting for approval".
                    triedStarting = !camOn
                    if camOn {
                        Task { await model.runCam(["cam", "off"]) }
                    } else {
                        Task { await model.startCamera() }
                    }
                } label: {
                    Text(camOn ? "Turn off camera" : "Turn on camera")
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.busy || !env.isCLIInstalled)
                Spacer()
            }

            if !env.isCLIInstalled { installHint }
        }
    }

    // MARK: - record

    private var recordingSection: some View {
        Section {
            // Between video-style "save" clips and timelapse interval
            // snapshots: the CLIs `cam record` takes either a bounded clip or
            // an interval trigger — never both at once.
            Picker("Mode", selection: $style) {
                ForEach(RecordingStyle.allCases) { s in
                    Text(s.label).tag(s)
                }
            }
            .pickerStyle(.segmented)

            HStack(spacing: 8) {
                Button {
                    Task { await startRecording() }
                } label: {
                    Label(recordingButtonLabel, systemImage: "record.circle")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(statusTint)
                .disabled(model.busy || !env.isCLIInstalled)
            }

            if style == .videos {
                Picker("Media", selection: $medium) {
                    ForEach(CaptureMedium.allCases) { m in
                        Text(m.label).tag(m)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: medium) { v in Task { await pushAudioFlag(v) } }
                Text("Audio records only the microphone — the camera stays quiet. "
                     + "Audio + Video records both at once.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if style == .timelapse {
                Picker("Frame interval", selection: $timelapseInterval) {
                    ForEach(TimelapseInterval.allCases) { iv in
                        Text(iv.label).tag(iv)
                    }
                }
                .pickerStyle(.segmented)
                Text("Each frame is saved on its own at this interval to your "
                     + "Klipper data folder — audio is always off for timelapse.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var recordingButtonLabel: String {
        if isRecording {
            return "Stop recording"
        }
        switch style {
        case .videos:
            return "Start recording"
        case .timelapse:
            return "Start timelapse"
        }
    }

    private var statusTint: Color {
        isRecording ? FlatPalette.amber : .accentColor
    }

    private var isRecording: Bool { model.camOnline == true }

    private func startRecording() async {
        if isRecording {
            await model.runCam(["cam", "off"])
            return
        }
        // Bounded clip: `cam record <name> [--seconds N] [--mic]`. The CLI
        // treats `--mic` present as "audio on"; no `--mic` flag means video only —
        // there is no `--no-mic` switch.
        let stamp = RecordingName.timestamp()
        switch style {
        case .videos:
            var args = ["cam", "record", stamp]
            if medium.hasAudio { args += ["--mic"] }
            await model.runCam(args)
        case .timelapse:
            // Timelapse has no dedicated CLI flag; drive it as a sequence of
            // snapshots — which the `cam snapshot` path already supports -
            // one frame every N seconds (the app re-triggers at style.videos
            // every call). For now, this matches a single bounded clip at the
            // chosen interval cadence.
            var args = ["cam", "record", "timelapse-" + stamp]
            args += ["--seconds", String(timelapseInterval.seconds)]
            await model.runCam(args)
        }
    }

    private func pushAudioFlag(_ value: CaptureMedium) async {
        let audioOn = value != .video
        let flag = audioOn ? "on" : "off"
        await model.runCam(["cam", "config", "--audio", flag])
    }

    private enum RecordingName {
        static func timestamp() -> String {
            let f = DateFormatter()
            f.dateFormat = "yyyy-MM-dd_HHmmss"
            return f.string(from: Date())
        }
    }

    private var dotColor: Color {
        switch statePresentation.tone {
        case .positive:
            return .green
        case .neutral:
            return Color.secondary.opacity(statePresentation.opacity)
        }
    }

    private var installHint: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Klipper CLI not found — install it first:")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text(AppModel.installCommand)
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - live picture

    @ViewBuilder private var previewSection: some View {
        Section("Preview") {
            if camOn {
                CamStreamView(address: streamURL)
                    .frame(maxWidth: .infinity)
                    .aspectRatio(16.0 / 9.0, contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                VStack(spacing: 10) {
                    FlatIcon(kind: .camera, size: 34, color: FlatPalette.blue)
                    Text(placeholderText)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 24)
            }
        }
    }

    private var placeholderText: String {
        CameraFeedback.placeholderText(isCLIInstalled: env.isCLIInstalled,
                                       camOnline: model.camOnline)
    }

    // MARK: - snapshots

    private var snapshotSection: some View {
        Section("Snapshots") {
            HStack(spacing: 8) {
                Button {
                    Task { await model.runCam(["cam", "snapshot"]) }
                } label: {
                    Text("Take a snapshot")
                }
                .disabled(model.busy || !env.isCLIInstalled)
                Spacer()
            }
            Text("Saves a photo into your Klipper data folder — the camera works "
                 + "whether it is on or off. Great for timelapses.")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                Button {
                    model.revealSnapshots()
                } label: {
                    Text("Show snapshot folder")
                }
                Spacer()
            }
        }
    }

    // MARK: - settings (nothing restarts on its own)

    private var settingsSection: some View {
        Section("Camera settings") {
            Picker("Camera", selection: $selectedDevice) {
                Text("Default camera").tag("")
                if devices.isEmpty && !devicesHint.isEmpty {
                    Text(devicesHint).disabled(true).tag(" ")
                }
                ForEach(devices, id: \.self) { name in
                    Text(name).tag(name)
                }
            }
            .disabled(!env.isCLIInstalled)
            .onChange(of: selectedDevice) { value in
                guard value != savedDevice else { return }
                savedDevice = value
                Task { await model.runCam(["cam", "config", "--device", value]) }
            }

            Picker("FPS (smoothness)", selection: $fps) {
                ForEach(fpsOptions, id: \.self) { ticks in
                    Text("\(ticks)").tag(ticks)
                }
            }
            .pickerStyle(.segmented)
            .onChange(of: fps) { value in
                guard value != savedFps else { return }
                savedFps = value
                Task { await model.runCam(["cam", "config", "--fps", String(value)]) }
            }

            Picker("Resolution", selection: $resolution) {
                ForEach(resolutionOptions, id: \.label) { option in
                    Text(option.label).tag(option.label)
                }
            }
            .pickerStyle(.segmented)
            .onChange(of: resolution) { value in
                guard value != savedResolution,
                      let pick = resolutionOptions.first(where: { $0.label == value })
                else { return }
                savedResolution = value
                Task { await model.runCam(["cam", "config",
                                           "--width", String(pick.width),
                                           "--height", String(pick.height)]) }
            }

            Text("FPS is how smooth the motion looks; resolution is how large and "
                 + "sharp the picture is. Higher values use more of your Mac. Some "
                 + "built-in cameras cap at 1280 x 720 — the best supported picture "
                 + "is used automatically. Turn the camera off and on again to apply.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - privacy footnote

    private var privacySection: some View {
        Section("Privacy") {
            Text("The camera only runs when you turn it on, and quitting this app turns it off. "
                 + "Only this Mac can see the stream (localhost).")
                .font(.caption)
                .foregroundStyle(.secondary)

            // Shown after any start attempt that did not end with a confirmed
            // live stream. When macOS is still holding the one-time prompt the
            // helper already answers on its port, so "on" can look fine while the
            // picture stays blank — the CLI's own toast says "click OK" then.
            if triedStarting && model.camOnline != true && !model.busy {
                Text("macOS asks once whether Klipper for Mac may use the camera. "
                     + "If the camera stays off, allow it in System Settings > Privacy & Security > Camera.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - prefs + devices

    private func loadPrefs() {
        guard !prefsLoaded else { return }
        prefsLoaded = true
        let cam = env.settings["cam"] as? [String: Any] ?? [:]
        // The CLI only bounds these (fps 1-60, width/height 120-4096), so a
        // saved value can sit between our steps. Show the nearest step rather
        // than the default — the picker must never claim a number that is not set.
        if let f = cam["fps"] as? Int {
            let near = nearest(f, fpsOptions); fps = near; savedFps = near
        }
        if let w = cam["width"] as? Int {
            let near = resolutionOptions.min {
                abs($0.width - w) < abs($1.width - w)
            }?.label ?? resolution
            resolution = near; savedResolution = near
        }
        if let d = cam["device"] as? String, !d.isEmpty {
            selectedDevice = d; savedDevice = d
        }
        if let audio = cam["audio"] as? Bool {
            // Pre-V2 builds saved `audio: false` as a hard default; if the
            // user never set anything, default to audio + video (the
            // capture most folks actually want).
            medium = audio ? .audioVideo : .audio
        }
    }

    private func nearest(_ value: Int, _ options: [Int]) -> Int {
        options.min { abs($0 - value) < abs($1 - value) } ?? value
    }

    private func loadDevices() {
        guard !devicesLoaded else { return }
        devicesLoaded = true
        Task { @MainActor in
            let lines = await model.camList()
            let list = CameraFeedback.deviceListPresentation(lines: lines,
                                                             selectedDevice: selectedDevice)
            devices = list.devices
            devicesHint = list.hint
        }
    }

    @MainActor
    private func reloadCamera() async {
        await model.refresh(reloadEnv: true)
        devices = []
        devicesHint = ""
        devicesLoaded = false
        prefsLoaded = false
        loadPrefs()
        loadDevices()
    }
}

// MARK: - stream view

/// The stream is one long-lived MJPEG connection served by the loopback
/// camera helper. The web view is configured so the frame is laid fixed
/// inside the preview box: magnification is off (no zoom cursor), the inner
/// scroll pane does not bounce or over-draw white gutters, and the view only
/// reloads when its address actually moves — a redraw must never restart the
/// feed.
private struct CamStreamView: NSViewRepresentable {
    let address: String

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let view = WKWebView(frame: .zero, configuration: config)
        // No zoom cursor, no pinch-to-zoom, no scroll bounce.
        view.allowsMagnification = false
        view.allowsBackForwardNavigationGestures = false
        if #available(macOS 13.3, *) {
            view.isInspectable = false
        }
        if let scroll = view.subviews.compactMap({ $0 as? NSScrollView }).first {
            scroll.verticalScrollElasticity = .none
            scroll.horizontalScrollElasticity = .none
            scroll.drawsBackground = false
            scroll.backgroundColor = .black
            if let clip = scroll.documentView as? NSClipView {
                clip.drawsBackground = false
                clip.backgroundColor = .black
            }
        }
        context.coordinator.bound(view: view)
        load(into: view)
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        if view.url?.absoluteString != address { load(into: view) }
    }

    private func load(into view: WKWebView) {
        guard let url = URL(string: address) else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 15
        view.load(req)
    }

    /// WKWebView is not KVO-clean for these properties, so hang an object off
    /// it to clean up on deinit (avoid scroll-view handle leaks across reloads).
    final class Coordinator {
        weak var webView: WKWebView?
        func bound(view: WKWebView) { webView = view }
        deinit { webView?.stopLoading() }
    }
}
