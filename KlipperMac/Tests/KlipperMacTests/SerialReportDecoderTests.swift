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

    func testSerialProbePolicyClassifiesDecodedReportAndLegacyAndKeep() {
        switch SerialProbePolicy.classify(output: SerialReportDecoder.contractFixture,
                                          exitCode: 0) {
        case .report(let report):
            XCTAssertEqual(report.mcus.first?.mcu, "mcu")
        default:
            XCTFail("fixture should classify as a decoded report")
        }

        let legacy = """
        usage: klipperformac serial [-h] [--set PATH [MCU]]
        klipperformac serial: error: unrecognized arguments: --json
        """
        XCTAssertEqual(SerialProbePolicy.classify(output: legacy, exitCode: 2),
                       .legacy)

        let keep = """
        usage: klipperformac serial [-h]
        klipperformac serial: error: timed out talking to the CLI helper
        """
        XCTAssertEqual(SerialProbePolicy.classify(output: keep, exitCode: 2),
                       .keep)
    }
}
