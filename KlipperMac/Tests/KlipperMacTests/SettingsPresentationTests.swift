import XCTest
@testable import KlipperMac

final class SettingsPresentationTests: XCTestCase {
    func testPinTextFallsBackToQuestionMarkWhenMissing() {
        XCTAssertEqual(SettingsPresentation.pinText([:], component: "klipper"), "?")
    }

    func testPinTextShortensPlainCommitPins() {
        let shaOnly = String(repeating: "a", count: 40)
        XCTAssertEqual(SettingsPresentation.pinText(["klipper": shaOnly], component: "klipper"),
                       "aaaaaaa (commit)")
    }

    func testPinTextShowsTagAndResolvedShaWhenBothExist() {
        XCTAssertEqual(
            SettingsPresentation.pinText([
                "moonraker": "v0.9.0",
                "moonraker_sha": "1234567890abcdef"
            ], component: "moonraker"),
            "v0.9.0  \u{00B7}  1234567"
        )
    }

    func testPinTextShowsResolvedCommitForShaPins() {
        let shaOnly = String(repeating: "a", count: 40)
        XCTAssertEqual(
            SettingsPresentation.pinText([
                "klipper": shaOnly,
                "klipper_sha": "bbbbbbbccccccc"
            ], component: "klipper"),
            "bbbbbbb  \u{00B7}  bbbbbbb"
        )
    }
}
