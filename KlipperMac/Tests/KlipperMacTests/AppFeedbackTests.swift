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
}
