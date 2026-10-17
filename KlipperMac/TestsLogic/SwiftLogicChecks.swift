import Foundation

@main
enum SwiftLogicChecks {
    static func main() {
        testSerialReportDecoder()
        testSerialReportDecoderPreambleAndNoise()
        testSerialProbePolicy()
        testAppFeedback()
        testCameraFeedback()
        testSettingsPresentation()
        testMoonrakerParsesVirtualSDcardProgress()
        testMoonrakerToleratesPartialQuery()
        testMoonrakerRejectsBrokenServerInfo()
        testMoonrakerIgnoresMalformedOptionalFields()
        testPrinterStatusReadinessNotes()
        testStackSnapshotOverall()
        testStackReaderTracksUnknownBootTimeRecords()
        testStackReaderListenerFallback()
        print("swift logic checks passed")
    }

    private static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() {
            fputs("swift logic check failed: \(message)\n", stderr)
            exit(1)
        }
    }

    private static func testSerialReportDecoder() {
        let report = SerialReportDecoder.decode(SerialReportDecoder.contractFixture)
        expect(report != nil, "decoder should accept pinned fixture")
        expect(report?.mcus.count == 7, "fixture should decode all MCU rows")
        expect(report?.mcus.first?.mcu == "mcu", "fixture master MCU should decode")

        let output = """
        usage: klipperformac serial [-h] [--set PATH [MCU]]
        klipperformac serial: error: unrecognized arguments: --json
        """
        expect(SerialReportDecoder.looksLikeLegacyCLI(output, exitCode: 2),
               "legacy parser rejection should be detected")
        expect(!SerialReportDecoder.looksLikeLegacyCLI("timed out", exitCode: 0),
               "transient garbage should not be treated as legacy")
    }

    private static func testSerialReportDecoderPreambleAndNoise() {
        let output = """
        warning: probe stderr
        \(SerialReportDecoder.contractFixture)
        """
        let report = SerialReportDecoder.decode(output)
        expect(report != nil, "decoder should accept a warning preamble around JSON")
        expect(report?.mcus.first?.mcu == "mcu",
               "decoder should keep the master row through a preamble")

        let usageOnly = """
        usage: klipperformac serial [-h]
        klipperformac serial: error: timed out talking to the CLI helper
        """
        expect(!SerialReportDecoder.looksLikeLegacyCLI(usageOnly, exitCode: 2),
               "generic usage noise without --json should not be treated as legacy")
    }

    private static func testSerialProbePolicy() {
        let report = SerialProbePolicy.classify(
            output: SerialReportDecoder.contractFixture, exitCode: 0)
        guard case .report(let decoded) = report else {
            expect(false, "fixture should classify as a report")
            return
        }
        expect(decoded.mcus.first?.mcu == "mcu",
               "report classification should keep decoded rows")

        let legacy = """
        usage: klipperformac serial [-h] [--set PATH [MCU]]
        klipperformac serial: error: unrecognized arguments: --json
        """
        expect(SerialProbePolicy.classify(output: legacy, exitCode: 2) == .legacy,
               "legacy argv rejection should classify as legacy")

        let keep = """
        usage: klipperformac serial [-h]
        klipperformac serial: error: timed out talking to the CLI helper
        """
        expect(SerialProbePolicy.classify(output: keep, exitCode: 2) == .keep,
               "generic noise should keep the last honest pane")
    }

    private static func testAppFeedback() {
        expect(AppFeedback.bannerMessage("   hello \n") == "hello",
               "banner message should trim surrounding whitespace")
        expect(AppFeedback.bannerMessage("   \n\t ") == nil,
               "blank banner text should disappear")
        let long = String(repeating: "x", count: 450)
        expect(AppFeedback.bannerMessage(long)?.count == 400,
               "banner message should cap toast length")

        let multiline = "\n   \n  first useful line \nsecond line"
        expect(AppFeedback.firstLine(multiline) == "first useful line",
               "firstLine should skip blank and whitespace-only lines")

        expect(AppFeedback.camToast(action: "snapshot", output: "") == "Saved snapshot",
               "snapshot without helper output should still confirm success")
        expect(AppFeedback.camToast(action: "on", output: "ask once in System Settings")
               == "macOS is asking for camera access — click OK.",
               "camera prompt guidance should be preserved")
        expect(AppFeedback.camToast(action: "other", output: "\n  \nline one\nline two")
               == "line one",
               "generic camera actions should use the first meaningful line")

        let successBanner = AppFeedback.bannerPresentation(message: "Saved", isError: false)
        expect(successBanner == BannerPresentation(title: "Done", message: "Saved",
                                                  tone: .positive),
               "success banner should render with Done/positive")
        let errorBanner = AppFeedback.bannerPresentation(message: "Problem text", isError: true)
        expect(errorBanner == BannerPresentation(title: "Problem",
                                                message: "Problem text",
                                                tone: .warning),
               "error banner should render with Problem/warning")
        expect(AppFeedback.bannerPresentation(message: nil, isError: false) == nil,
               "missing banner messages should not render")

        expect(AppFeedback.statusBadgePresentation(overall: .running)
               == StatusBadgePresentation(text: "Running", tone: .positive),
               "running status badge should be positive")
        expect(AppFeedback.statusBadgePresentation(overall: .notResponding)
               == StatusBadgePresentation(text: "Not responding", tone: .warning),
               "notResponding status badge should be warning")
        expect(AppFeedback.statusBadgePresentation(overall: .stopped)
               == StatusBadgePresentation(text: "Stopped", tone: .neutral),
               "stopped status badge should be neutral")

        expect(AppFeedback.inlineWarning(message: "  printer not ready \n")
               == InlineWarningPresentation(message: "printer not ready",
                                            tone: .warning),
               "inline warning should trim and preserve warning tone")
        expect(AppFeedback.inlineWarning(message: " \n\t ") == nil,
               "blank warning rows should not render")

        expect(AppFeedback.progressPresentation(0.625)
               == ProgressPresentation(fraction: 0.625, label: "63%"),
               "progress presentation should keep in-range values")
        expect(AppFeedback.progressPresentation(1.7)
               == ProgressPresentation(fraction: 1, label: "100%"),
               "progress presentation should clamp high values")
        expect(AppFeedback.progressPresentation(-0.2)
               == ProgressPresentation(fraction: 0, label: "0%"),
               "progress presentation should clamp low values")
        expect(AppFeedback.progressPresentation(nil) == nil,
               "missing progress should not render")

        let report = SerialReportDecoder.decode(SerialReportDecoder.contractFixture)
        let rows = report?.mcus ?? []
        let badSection = rows.first { $0.mcu == " board" }
        expect(badSection?.pickerStatusPresentation == nil,
               "bad section names should not show a CAN/fixed label")
        expect(badSection?.footerPresentation
               == InlineWarningPresentation(message: "name cannot be set from here — edit printer.cfg",
                                            tone: .warning),
               "bad section names should surface a warning footer")
        expect(badSection?.badgePresentations.last
               == RowBadgePresentation(text: "bad section",
                                       tone: .warning,
                                       help: McuConflict.badSectionName.tooltip,
                                       showsWarningIcon: true),
               "bad section names should surface a warning badge presentation")

        let fixed = rows.first { $0.mcu == "rpi" }
        expect(fixed?.pickerStatusPresentation
               == PickerStatusPresentation(
                    label: "CAN / fixed",
                    help: "This row is configured through a non-USB identity, so the GUI keeps it read-only."),
               "non-USB-configured rows should show the fixed/CAN status presentation")
        expect(fixed?.footerPresentation
               == InlineWarningPresentation(message: "set here in printer.cfg, not from a USB picker",
                                            tone: .neutral),
               "non-USB-configured rows should explain the read-only picker")
        expect(fixed?.pickerHelpText.contains(fixed?.section ?? "") == true,
               "picker help should keep the section context")

        let master = rows.first { $0.mcu == "mcu" }
        expect(master?.badgePresentations.first
               == RowBadgePresentation(text: "timing master",
                                       tone: .accent,
                                       help: nil,
                                       showsWarningIcon: false),
               "master rows should surface their accent badge presentation")

        let missing = rows.first { $0.mcu == "tool" }
        expect(missing?.identityPresentation
               == IdentityPresentation(
                    text: "USB 1a86:7523  SER FFFF0001  ·  present: n",
                    present: false),
               "identity presentation should keep the offline text and presence flag")
        expect(missing?.identityIndicatorPresentation
               == IdentityIndicatorPresentation(
                    text: "USB 1a86:7523  SER FFFF0001  ·  present: n",
                    showsIndicator: true,
                    tone: .warning,
                    opacity: 0.7),
               "identity indicator presentation should keep the offline warning dot")

        let noIdentity = rows.first { $0.mcu == "camera" }
        expect(noIdentity?.identityIndicatorPresentation
               == IdentityIndicatorPresentation(
                    text: "identity: not detected",
                    showsIndicator: false,
                    tone: .warning,
                    opacity: 0),
               "missing identities should suppress the presence dot")
    }

    private static func testCameraFeedback() {
        expect(CameraFeedback.statePresentation(isOnline: true)
               == CameraStatePresentation(text: "Camera is on",
                                          kind: .on,
                                          tone: .positive,
                                          opacity: 1),
               "camera state should show on when the helper is online")
        expect(CameraFeedback.statePresentation(isOnline: false)
               == CameraStatePresentation(text: "Camera is off",
                                          kind: .off,
                                          tone: .neutral,
                                          opacity: 1),
               "camera state should show off when the helper is offline")
        expect(CameraFeedback.statePresentation(isOnline: nil)
               == CameraStatePresentation(text: "Camera status unknown — press Refresh",
                                          kind: .unknown,
                                          tone: .neutral,
                                          opacity: 0.45),
               "camera state should guide the user to refresh when status is unknown")

        expect(CameraFeedback.placeholderText(isCLIInstalled: false, camOnline: nil)
               == "Install the Klipper CLI to use the camera.",
               "camera placeholder should explain the missing CLI")
        expect(CameraFeedback.placeholderText(isCLIInstalled: true, camOnline: nil)
               == "Press Refresh to check the camera.",
               "camera placeholder should guide the first refresh")
        expect(CameraFeedback.placeholderText(isCLIInstalled: true, camOnline: false)
               == "Turn the camera on to see the live picture.",
               "camera placeholder should guide the user to start the camera")

        let listOutput = """
        Waiting for camera permission — click OK in the macOS dialog.
        0\tFaceTime HD Camera\t1280x720\t30
        not-a-device row
        1\tUSB Camera\t1920x1080\t60
        1\tUSB Camera\t1920x1080\t60
        """
        expect(CameraFeedback.deviceNames(fromListOutput: listOutput)
               == ["FaceTime HD Camera", "USB Camera"],
               "camera list parsing should keep only indexed device rows once")

        let hinted = CameraFeedback.deviceListPresentation(lines: [CameraFeedback.listFailed],
                                                           selectedDevice: "")
        expect(hinted == CameraDeviceListPresentation(devices: [],
                                                      hint: CameraFeedback.listFailed),
               "camera list presentation should preserve guidance hints")

        let saved = CameraFeedback.deviceListPresentation(lines: ["USB Camera"],
                                                          selectedDevice: "Saved Camera")
        expect(saved == CameraDeviceListPresentation(devices: ["Saved Camera", "USB Camera"],
                                                     hint: ""),
               "camera list presentation should keep the saved selection visible")
    }

    private static func testSettingsPresentation() {
        expect(SettingsPresentation.pinText([:], component: "klipper") == "?",
               "missing component pins should fall back to a question mark")

        let shaOnly = String(repeating: "a", count: 40)
        expect(SettingsPresentation.pinText(["klipper": shaOnly], component: "klipper")
               == "aaaaaaa (commit)",
               "sha pins without a saved checkout sha should render as a short commit")

        let tagged = SettingsPresentation.pinText([
            "moonraker": "v0.9.0",
            "moonraker_sha": "1234567890abcdef"
        ], component: "moonraker")
        expect(tagged == "v0.9.0  \u{00B7}  1234567",
               "tag pins with a saved checkout sha should show both values")

        let shaBacked = SettingsPresentation.pinText([
            "klipper": shaOnly,
            "klipper_sha": "bbbbbbbccccccc"
        ], component: "klipper")
        expect(shaBacked == "bbbbbbb  \u{00B7}  bbbbbbb",
               "sha pins with a saved checkout sha should show the resolved commit twice")
    }

    private static func testMoonrakerParsesVirtualSDcardProgress() {
        let info: [String: Any] = [
            "result": [
                "klippy_connected": true,
                "klippy_state": "ready",
            ]
        ]
        let query: [String: Any] = [
            "result": [
                "status": [
                    "print_stats": [
                        "state": "printing",
                        "filename": "cube.gcode",
                        "message": "printing"
                    ],
                    "virtual_sdcard": [
                        "progress": 0.625
                    ],
                    "gcode_move": [
                        "gcode_position": [10, 20, 0.3, 0]
                    ]
                ]
            ]
        ]
        let status = Moonraker.status(info: info, query: query)
        expect(status != nil, "status should parse valid payloads")
        expect(status?.connected == true, "connected should follow /server/info")
        expect(status?.klippyState == "ready", "klippy_state should parse")
        expect(status?.printState == "printing", "print state should parse")
        expect(status?.filename == "cube.gcode", "filename should parse")
        expect(status?.progress == 0.625, "progress should come from virtual_sdcard")
        expect(status?.position == Position(x: 10, y: 20, z: 0.3),
               "position should accept mixed int/double arrays")
    }

    private static func testMoonrakerToleratesPartialQuery() {
        let info: [String: Any] = [
            "result": [
                "klippy_connected": false,
                "klippy_state": "startup",
            ]
        ]
        let query: [String: Any] = [
            "result": [
                "status": [
                    "print_stats": [
                        "state": "standby"
                    ]
                ]
            ]
        ]
        let status = Moonraker.status(info: info, query: query)
        expect(status != nil, "partial query should still produce a status")
        expect(status?.connected == false, "partial query should keep info fields")
        expect(status?.progress == nil, "missing virtual_sdcard should leave progress nil")
        expect(status?.position == nil, "missing gcode_move should leave position nil")
    }

    private static func testMoonrakerRejectsBrokenServerInfo() {
        let status = Moonraker.status(info: [:], query: nil)
        expect(status == nil, "status should be nil when /server/info has no result payload")
    }

    private static func testMoonrakerIgnoresMalformedOptionalFields() {
        let info: [String: Any] = [
            "result": [
                "klippy_connected": true,
                "klippy_state": "startup",
            ]
        ]
        let query: [String: Any] = [
            "result": [
                "status": [
                    "print_stats": [
                        "state": "paused",
                        "filename": 1234,
                        "message": ["unexpected": "shape"],
                    ],
                    "virtual_sdcard": [
                        "progress": "half"
                    ],
                    "gcode_move": [
                        "gcode_position": [10, 20]
                    ]
                ]
            ]
        ]
        let status = Moonraker.status(info: info, query: query)
        expect(status != nil, "malformed optional fields should not drop the whole status")
        expect(status?.connected == true, "connected should still parse")
        expect(status?.klippyState == "startup", "klippy_state should still parse")
        expect(status?.printState == "paused", "print state should still parse")
        expect(status?.filename == nil, "non-string filename should be ignored")
        expect(status?.message == nil, "non-string message should be ignored")
        expect(status?.progress == nil, "non-double progress should be ignored")
        expect(status?.position == nil, "short positions should be ignored")
    }

    private static func testPrinterStatusReadinessNotes() {
        let ready = PrinterStatus(connected: true, klippyState: "ready")
        expect(ready.connectivityLabel == "connected (ready)",
               "ready printer should describe connected ready state")
        expect(ready.readinessNote == nil,
               "ready printer should not show a degraded note")

        let startup = PrinterStatus(connected: false, klippyState: "startup")
        expect(startup.readinessNote?.contains("starting up") == true,
               "startup printer should explain that the stack can be running first")
        expect(startup.stackWarning?.contains("Printer not ready (startup)") == true,
               "startup printer should expose a stack-level degraded summary")

        let error = PrinterStatus(connected: false, klippyState: "error")
        expect(error.readinessNote?.contains("reported an error") == true,
               "error printer should show a stronger degraded note")
        expect(error.stackWarning?.contains("Printer not ready (error)") == true,
               "error printer should expose a stack-level degraded summary")

        let shutdown = PrinterStatus(connected: false, klippyState: "shutdown")
        expect(shutdown.readinessNote?.contains("needs a restart or firmware recovery") == true,
               "shutdown printer should explain the recovery path")
        expect(shutdown.stackWarning?.contains("Printer not ready (shutdown)") == true,
               "shutdown printer should expose a stack-level degraded summary")

        let connectedButPaused = PrinterStatus(connected: true, klippyState: "paused")
        expect(connectedButPaused.readinessNote == "Klippy is connected but not ready.",
               "non-ready connected printer should still show a degraded note")
        expect(connectedButPaused.stackWarning?.contains("Printer not ready (paused)") == true,
               "connected non-ready printer should still warn at stack level")
    }

    private static func testStackSnapshotOverall() {
        var snap = StackSnapshot()
        expect(snap.overall == .stopped, "untracked snapshot should be stopped")
        snap.tracked = true
        snap.services = [ServiceState(name: "klipper", up: false)]
        expect(snap.overall == .notResponding, "tracked snapshot with no services up should degrade")
        snap.services = [ServiceState(name: "klipper", up: true)]
        expect(snap.overall == .running, "tracked snapshot with one service up should be running")

        snap = StackSnapshot(listenerPorts: [7125])
        expect(snap.anyUp, "listener fallback should count as running evidence")
        expect(snap.overall == .running,
               "untracked listener ports should keep the snapshot running")
    }

    private static func testStackReaderTracksUnknownBootTimeRecords() {
        let markers = StackReader.markers(webPort: 8080, fluiddPort: 8081)
        let tracked = StackReader.snapshot(
            from: [
                "boot_time": StackReader.bootUnknown,
                "started": "2026-10-17T12:00:00",
                "klipper": 4242
            ],
            bootTime: nil,
            markers: markers,
            commands: ["4242": "/usr/bin/python klippy.py -a sock"]
        )
        expect(tracked.tracked, "boot_time unknown should still count as a tracked stack record")
        expect(tracked.startedAt == "2026-10-17T12:00:00",
               "tracked snapshot should preserve startedAt")
        expect(tracked.overall == .running,
               "matching command marker should keep the snapshot running")

        let stale = StackReader.snapshot(
            from: ["boot_time": "old-boot", "klipper": 4242],
            bootTime: "new-boot",
            markers: markers,
            commands: ["4242": "/usr/bin/python klippy.py -a sock"]
        )
        expect(!stale.tracked, "foreign boot records should read as stopped")

        let mismatched = StackReader.snapshot(
            from: ["boot_time": StackReader.bootUnknown, "klipper": 4242],
            bootTime: nil,
            markers: markers,
            commands: ["4242": "/usr/bin/python something-else.py"]
        )
        expect(mismatched.tracked, "unknown boot records still belong to this session")
        expect(mismatched.overall == .notResponding,
               "a tracked record whose marker no longer matches should degrade")
    }

    private static func testStackReaderListenerFallback() {
        let markers = StackReader.markers(webPort: 8080, fluiddPort: 8081)
        let ports = StackReader.listenerPorts(
            from: [
                7125: ["4242"],
                8080: ["9000"],
                8081: ["4243"],
            ],
            markers: markers,
            commands: [
                "4242": "/usr/bin/python -m moonraker",
                "9000": "/usr/bin/python -m http.server 8080",
                "4243": "/usr/bin/python -m klipperformac.proxy 8081 /tmp/fluidd 7125 127.0.0.1",
            ]
        )
        expect(ports == Set([7125, 8081]),
               "listener fallback should keep only ports held by our services")
    }
}
