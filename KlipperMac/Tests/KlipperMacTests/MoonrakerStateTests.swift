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

    func testStatusRequiresServerInfoResult() {
        XCTAssertNil(Moonraker.status(info: [:], query: nil))
    }

    func testPrinterStatusReadinessNotesCoverDegradedStates() {
        XCTAssertNil(PrinterStatus(connected: true, klippyState: "ready").readinessNote)

        let startup = PrinterStatus(connected: false, klippyState: "startup")
        XCTAssertTrue(startup.readinessNote?.contains("starting up") == true)
        XCTAssertTrue(startup.stackWarning?.contains("Printer not ready (startup)") == true)

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
}
