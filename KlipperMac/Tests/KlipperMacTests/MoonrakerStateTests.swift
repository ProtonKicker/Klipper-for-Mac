import XCTest
@testable import KlipperMac

final class MoonrakerStateTests: XCTestCase {
    func testStatusParsesProgressFromVirtualSDcard() {
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
        XCTAssertEqual(status?.connected, true)
        XCTAssertEqual(status?.klippyState, "ready")
        XCTAssertEqual(status?.printState, "printing")
        XCTAssertEqual(status?.filename, "cube.gcode")
        XCTAssertEqual(status?.progress, 0.625)
        XCTAssertEqual(status?.position, Position(x: 10, y: 20, z: 0.3))
    }

    func testStatusToleratesPartialAndMalformedOptionalFields() {
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
        XCTAssertEqual(status?.connected, true)
        XCTAssertEqual(status?.klippyState, "startup")
        XCTAssertEqual(status?.printState, "paused")
        XCTAssertNil(status?.filename)
        XCTAssertNil(status?.message)
        XCTAssertNil(status?.progress)
        XCTAssertNil(status?.position)
    }

    func testStatusToleratesPartialQueryWithoutOptionalObjects() {
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
        XCTAssertEqual(status?.connected, false)
        XCTAssertEqual(status?.klippyState, "startup")
        XCTAssertEqual(status?.printState, "standby")
        XCTAssertNil(status?.progress)
        XCTAssertNil(status?.position)
    }

    func testStatusRequiresServerInfoResult() {
        XCTAssertNil(Moonraker.status(info: [:], query: nil))
    }

    func testPrinterStatusReadinessNotesCoverDegradedStates() {
        XCTAssertNil(PrinterStatus(connected: true, klippyState: "ready").readinessNote)

        let startup = PrinterStatus(connected: false, klippyState: "startup")
        XCTAssertTrue(startup.readinessNote?.contains("starting up") == true)
        XCTAssertTrue(startup.stackWarning?.contains("Printer not ready (startup)") == true)

        let error = PrinterStatus(connected: false, klippyState: "error")
        XCTAssertTrue(error.readinessNote?.contains("reported an error") == true)
        XCTAssertTrue(error.stackWarning?.contains("Printer not ready (error)") == true)

        let shutdown = PrinterStatus(connected: false, klippyState: "shutdown")
        XCTAssertTrue(shutdown.readinessNote?.contains("firmware recovery") == true)
        XCTAssertTrue(shutdown.stackWarning?.contains("Printer not ready (shutdown)") == true)

        let paused = PrinterStatus(connected: true, klippyState: "paused")
        XCTAssertEqual(paused.readinessNote, "Klippy is connected but not ready.")
        XCTAssertTrue(paused.stackWarning?.contains("Printer not ready (paused)") == true)
    }

    func testStackSnapshotOverallTracksStoppedAndNotResponding() {
        var snap = StackSnapshot()
        XCTAssertEqual(snap.overall, .stopped)

        snap.tracked = true
        snap.services = [ServiceState(name: "klipper", up: false)]
        XCTAssertEqual(snap.overall, .notResponding)

        snap.services = [ServiceState(name: "klipper", up: true)]
        XCTAssertEqual(snap.overall, .running)
    }

    func testStackReaderSnapshotAcceptsUnknownBootTimeAndRejectsForeignBoots() {
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
        XCTAssertTrue(tracked.tracked)
        XCTAssertEqual(tracked.startedAt, "2026-10-17T12:00:00")
        XCTAssertEqual(tracked.overall, .running)

        let stale = StackReader.snapshot(
            from: ["boot_time": "old-boot", "klipper": 4242],
            bootTime: "new-boot",
            markers: markers,
            commands: ["4242": "/usr/bin/python klippy.py -a sock"]
        )
        XCTAssertFalse(stale.tracked)
        XCTAssertEqual(stale.overall, .stopped)
    }
}
