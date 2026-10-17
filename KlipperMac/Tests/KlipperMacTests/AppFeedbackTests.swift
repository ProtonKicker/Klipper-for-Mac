import XCTest
@testable import KlipperMac

final class AppFeedbackTests: XCTestCase {
    func testBannerMessageTrimsAndCapsLength() {
        XCTAssertEqual(AppFeedback.bannerMessage("   hello \n"), "hello")
        XCTAssertNil(AppFeedback.bannerMessage("   \n\t "))
        XCTAssertEqual(AppFeedback.bannerMessage(String(repeating: "x", count: 450))?.count,
                       400)
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
}
