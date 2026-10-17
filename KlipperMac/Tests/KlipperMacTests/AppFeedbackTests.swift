import XCTest
@testable import KlipperMac

final class AppFeedbackTests: XCTestCase {
    func testBannerMessageTrimsAndCapsLength() {
        XCTAssertEqual(AppFeedback.bannerMessage("   hello \n"), "hello")
        XCTAssertNil(AppFeedback.bannerMessage("   \n\t "))
        XCTAssertEqual(AppFeedback.bannerMessage(String(repeating: "x", count: 450))?.count,
                       400)
    }

    func testBannerPresentationChoosesTitleAndToneFromSeverity() {
        XCTAssertEqual(AppFeedback.bannerPresentation(message: "Saved", isError: false),
                       BannerPresentation(title: "Done", message: "Saved",
                                          tone: .positive))
        XCTAssertEqual(AppFeedback.bannerPresentation(message: "Problem text", isError: true),
                       BannerPresentation(title: "Problem", message: "Problem text",
                                          tone: .warning))
        XCTAssertNil(AppFeedback.bannerPresentation(message: nil, isError: false))
    }

    func testFirstLineSkipsBlankAndWhitespaceOnlyLines() {
        let text = "\n   \n  first useful line \nsecond line"
        XCTAssertEqual(AppFeedback.firstLine(text), "first useful line")
    }

    func testCamToastHandlesBlankSnapshotAndPromptGuidance() {
        XCTAssertEqual(AppFeedback.camToast(action: "snapshot", output: ""),
                       "Saved snapshot")
        XCTAssertEqual(AppFeedback.camToast(action: "on",
                                            output: "ask once in System Settings"),
                       "macOS is asking for camera access — click OK.")
        XCTAssertEqual(AppFeedback.camToast(action: "other",
                                            output: "\n \nline one\nline two"),
                       "line one")
    }

    func testStatusBadgePresentationMapsOverallStatesToTextAndTone() {
        XCTAssertEqual(AppFeedback.statusBadgePresentation(overall: .running),
                       StatusBadgePresentation(text: "Running", tone: .positive))
        XCTAssertEqual(AppFeedback.statusBadgePresentation(overall: .notResponding),
                       StatusBadgePresentation(text: "Not responding", tone: .warning))
        XCTAssertEqual(AppFeedback.statusBadgePresentation(overall: .stopped),
                       StatusBadgePresentation(text: "Stopped", tone: .neutral))
    }

    func testInlineWarningTrimsAndSuppressesBlankMessages() {
        XCTAssertEqual(AppFeedback.inlineWarning(message: "  printer not ready \n"),
                       InlineWarningPresentation(message: "printer not ready",
                                                 tone: .warning))
        XCTAssertNil(AppFeedback.inlineWarning(message: " \n\t "))
    }

    func testProgressPresentationClampsFractionAndLabelTogether() {
        XCTAssertEqual(AppFeedback.progressPresentation(0.625),
                       ProgressPresentation(fraction: 0.625, label: "63%"))
        XCTAssertEqual(AppFeedback.progressPresentation(1.7),
                       ProgressPresentation(fraction: 1, label: "100%"))
        XCTAssertEqual(AppFeedback.progressPresentation(-0.2),
                       ProgressPresentation(fraction: 0, label: "0%"))
        XCTAssertNil(AppFeedback.progressPresentation(nil))
    }
}
