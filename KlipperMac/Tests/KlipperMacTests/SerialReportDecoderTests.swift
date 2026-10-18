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

    func testGenericUsageNoiseWithoutJsonIsNotTreatedAsLegacy() {
        let output = """
        usage: klipperformac serial [-h]
        klipperformac serial: error: timed out talking to the CLI helper
        """
        XCTAssertFalse(SerialReportDecoder.looksLikeLegacyCLI(output, exitCode: 2))
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

    func testMcuRowPresentationPolicyExposesFooterAndPickerHints() {
        let report = SerialReportDecoder.decode(SerialReportDecoder.contractFixture)
        let rows = report?.mcus ?? []

        let badSection = rows.first { $0.mcu == " board" }
        XCTAssertNil(badSection?.pickerStatusPresentation)
        XCTAssertEqual(badSection?.footerPresentation,
                       InlineWarningPresentation(
                        message: "name cannot be set from here — edit printer.cfg",
                        tone: .warning))
        XCTAssertEqual(badSection?.badgePresentations.last,
                       RowBadgePresentation(
                        text: "bad section",
                        tone: .warning,
                        help: McuConflict.badSectionName.tooltip,
                        showsWarningIcon: true))

        let fixed = rows.first { $0.mcu == "rpi" }
        XCTAssertEqual(fixed?.pickerStatusPresentation,
                       PickerStatusPresentation(
                        label: "CAN / fixed",
                        help: "This row is configured through a non-USB identity, so the GUI keeps it read-only."))
        XCTAssertEqual(fixed?.footerPresentation,
                       InlineWarningPresentation(
                        message: "set here in printer.cfg, not from a USB picker",
                        tone: .neutral))
        XCTAssertTrue(fixed?.pickerHelpText.contains(fixed?.section ?? "") == true)

        let master = rows.first { $0.mcu == "mcu" }
        XCTAssertEqual(master?.badgePresentations.first,
                       RowBadgePresentation(
                        text: "timing master",
                        tone: .accent,
                        help: nil,
                        showsWarningIcon: false))

        let missing = rows.first { $0.mcu == "tool" }
        XCTAssertEqual(missing?.identityPresentation,
                       IdentityPresentation(
                        text: "USB 1a86:7523  SER FFFF0001  ·  present: n",
                        present: false))
        XCTAssertEqual(missing?.identityIndicatorPresentation,
                       IdentityIndicatorPresentation(
                        text: "USB 1a86:7523  SER FFFF0001  ·  present: n",
                        showsIndicator: true,
                        tone: .warning,
                        opacity: 0.7))

        let noIdentity = rows.first { $0.mcu == "camera" }
        XCTAssertEqual(noIdentity?.identityIndicatorPresentation,
                       IdentityIndicatorPresentation(
                        text: "identity: not detected",
                        showsIndicator: false,
                        tone: .warning,
                        opacity: 0))
    }

    func testMcuRowPickerDefaultsToConfiguredOrSingleDetectedPort() {
        let report = SerialReportDecoder.decode(SerialReportDecoder.contractFixture)
        let rows = report?.mcus ?? []

        let master = rows.first { $0.mcu == "mcu" }
        let masterPorts = [
            SerialPort(path: "/dev/cu.usbmodem142101",
                       label: "USB-CDC (STM32 / SKR / Pi Pico class boards)"),
            SerialPort(path: "/dev/cu.usbmodem14101",
                       label: "USB-CDC (STM32 / SKR / Pi Pico class boards)")
        ]
        XCTAssertEqual(master?.suggestedPort(in: masterPorts,
                                             allowSinglePortDefault: false)?.path,
                       "/dev/cu.usbmodem14101")
        XCTAssertEqual(master?.pickerButtonText(in: masterPorts,
                                                allowSinglePortDefault: false),
                       "/dev/cu.usbmodem14101")

        let tool = rows.first { $0.mcu == "tool" }
        let singlePort = [
            SerialPort(path: "/dev/cu.usbmodem142101",
                       label: "USB-CDC (STM32 / SKR / Pi Pico class boards)")
        ]
        XCTAssertNil(tool?.suggestedPort(in: singlePort,
                                         allowSinglePortDefault: false))
        XCTAssertEqual(tool?.suggestedPort(in: singlePort,
                                           allowSinglePortDefault: true)?.path,
                       "/dev/cu.usbmodem142101")
        XCTAssertEqual(tool?.pickerButtonText(in: singlePort,
                                              allowSinglePortDefault: true),
                       "/dev/cu.usbmodem142101")
    }
}
