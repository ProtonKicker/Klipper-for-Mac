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
    @State private var quality = 80
    @State private var savedQuality = 80

    private let fpsOptions = [10, 15, 20, 25, 30]
    private let qualityOptions = [60, 70, 80, 90]

    private var env: KEnvironment { model.env }
    private var camOn: Bool { model.camOnline == true }
    private var streamURL: String { "\(env.camURL)/?action=stream" }

    var body: some View {
        Form {
            cameraSection
            previewSection
            snapshotSection
            settingsSection
            privacySection
        }
        .formStyle(.grouped)
        .navigationTitle("Camera")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await model.refresh(reloadEnv: true) }
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
                Text(stateText)
                    .font(.callout.weight(.medium))
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
            }

            HStack(spacing: 8) {
                Button {
                    // Only a start attempt can mean "macOS is waiting for approval".
                    triedStarting = !camOn
                    Task { await model.runCam(camOn ? ["cam", "off"] : ["cam", "on"]) }
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

    private var stateText: String {
        guard let on = model.camOnline else { return "Camera status unknown — press Refresh" }
        return on ? "Camera is on" : "Camera is off"
    }

    private var dotColor: Color {
        guard let on = model.camOnline else { return Color.secondary.opacity(0.45) }
        return on ? .green : .secondary
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
        if !env.isCLIInstalled { return "Install the Klipper CLI to use the camera." }
        return model.camOnline == nil
            ? "Press Refresh to check the camera."
            : "Turn the camera on to see the live picture."
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

            Picker("Picture smoothness", selection: $fps) {
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

            Picker("Picture quality", selection: $quality) {
                ForEach(qualityOptions, id: \.self) { level in
                    Text("\(level)%").tag(level)
                }
            }
            .pickerStyle(.segmented)
            .onChange(of: quality) { value in
                guard value != savedQuality else { return }
                savedQuality = value
                Task { await model.runCam(["cam", "config", "--quality", String(value)]) }
            }

            Text("Smoothness is how often the picture updates. Quality is how sharp it looks. "
                 + "Higher values use more of your Mac. Turn the camera off and on again to apply.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - privacy footnote

    private var privacySection: some View {
        Section("Privacy") {
            Text("The camera only runs when you turn it on. Only this Mac can see the stream (localhost).")
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
        // The CLI only bounds these (fps 1-60, quality 1-100), so a saved value
        // can sit between our steps. Show the nearest step rather than the
        // default — the picker must never claim a number that is not set.
        if let f = cam["fps"] as? Int {
            let near = nearest(f, fpsOptions); fps = near; savedFps = near
        }
        if let q = cam["quality"] as? Int {
            let near = nearest(q, qualityOptions); quality = near; savedQuality = near
        }
        if let d = cam["device"] as? String, !d.isEmpty {
            selectedDevice = d; savedDevice = d
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
            let names = lines.filter { !AppModel.camGuidance.contains($0) }
            // A saved camera may not be plugged in right now — keep it listed so
            // the picker still shows what the user chose.
            if !selectedDevice.isEmpty, !names.contains(selectedDevice) {
                devices = [selectedDevice] + names
            } else {
                devices = names
            }
            if names.isEmpty {
                devicesHint = lines.first(where: { AppModel.camGuidance.contains($0) })
                    ?? "No camera found — plug one in, or use the camera built into this Mac"
            }
        }
    }
}

// MARK: - stream view

/// The stream is one long-lived connection, so the web view only reloads when the
/// address actually moved — a redraw must never restart the feed.
private struct CamStreamView: NSViewRepresentable {
    let address: String

    func makeNSView(context: Context) -> WKWebView {
        let view = WKWebView()
        load(into: view)
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        guard view.url?.absoluteString != address else { return }
        load(into: view)
    }

    private func load(into view: WKWebView) {
        guard let url = URL(string: address) else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 15
        view.load(req)
    }
}
