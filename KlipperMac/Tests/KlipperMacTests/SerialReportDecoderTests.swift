import XCTest
@testable import KlipperMac

final class SerialReportDecoderTests: XCTestCase {
    func testDecoderAcceptsThePinnedContractFixture() {
        let report = SerialReportDecoder.decode(SerialReportDecoder.contractFixture)
        XCTAssertNotNil(report)
        XCTAssertEqual(report?.mcus.count, 7)
        XCTAssertEqual(report?.mcus.first?.mcu, "mcu")
    }

    func testDecoderAcceptsAWarningPreambleAroundJson() {
        let output = """
        warning: probe stderr
        \(SerialReportDecoder.contractFixture)
        """
        let report = SerialReportDecoder.decode(output)
        XCTAssertNotNil(report)
        XCTAssertEqual(report?.mcus.first?.mcu, "mcu")
    }

    func testLegacyDetectionMatchesArgumentParserFailure() {
        let output = """
        usage: klipperformac serial [-h] [--set PATH [MCU]]
        klipperformac serial: error: unrecognized arguments: --json
        """
        XCTAssertTrue(SerialReportDecoder.looksLikeLegacyCLI(output, exitCode: 2))
        XCTAssertNil(SerialReportDecoder.decode(output))
    }

    func testTransientGarbageIsNotTreatedAsLegacy() {
        let output = "timed out while waiting for serial --json"
        XCTAssertFalse(SerialReportDecoder.looksLikeLegacyCLI(output, exitCode: 0))
        XCTAssertNil(SerialReportDecoder.decode(output))
    }
}
