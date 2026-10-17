import Foundation

@main
enum SwiftLogicChecks {
    static func main() {
        testSerialReportDecoder()
        testMoonrakerParsesVirtualSDcardProgress()
        testMoonrakerToleratesPartialQuery()
        testStackSnapshotOverall()
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

    private static func testStackSnapshotOverall() {
        var snap = StackSnapshot()
        expect(snap.overall == .stopped, "untracked snapshot should be stopped")
        snap.tracked = true
        snap.services = [ServiceState(name: "klipper", up: false)]
        expect(snap.overall == .notResponding, "tracked snapshot with no services up should degrade")
        snap.services = [ServiceState(name: "klipper", up: true)]
        expect(snap.overall == .running, "tracked snapshot with one service up should be running")
    }
}
