import Foundation

@main
enum SwiftLogicChecks {
    static func main() {
        testSerialReportDecoder()
        testSerialReportDecoderPreambleAndNoise()
        testSerialProbePolicy()
        testMoonrakerParsesVirtualSDcardProgress()
        testMoonrakerToleratesPartialQuery()
        testMoonrakerRejectsBrokenServerInfo()
        testMoonrakerIgnoresMalformedOptionalFields()
        testPrinterStatusReadinessNotes()
        testStackSnapshotOverall()
        testStackReaderTracksUnknownBootTimeRecords()
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
}
