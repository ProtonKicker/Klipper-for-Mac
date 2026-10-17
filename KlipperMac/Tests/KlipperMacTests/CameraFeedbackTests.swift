import XCTest
@testable import KlipperMac

final class CameraFeedbackTests: XCTestCase {
    func testStatePresentationCoversOnOffAndUnknown() {
        XCTAssertEqual(CameraFeedback.statePresentation(isOnline: true),
                       CameraStatePresentation(text: "Camera is on", kind: .on))
        XCTAssertEqual(CameraFeedback.statePresentation(isOnline: false),
                       CameraStatePresentation(text: "Camera is off", kind: .off))
        XCTAssertEqual(CameraFeedback.statePresentation(isOnline: nil),
                       CameraStatePresentation(text: "Camera status unknown — press Refresh",
                                               kind: .unknown))
    }

    func testPlaceholderTextGuidesInstallRefreshAndStart() {
        XCTAssertEqual(CameraFeedback.placeholderText(isCLIInstalled: false, camOnline: nil),
                       "Install the Klipper CLI to use the camera.")
        XCTAssertEqual(CameraFeedback.placeholderText(isCLIInstalled: true, camOnline: nil),
                       "Press Refresh to check the camera.")
        XCTAssertEqual(CameraFeedback.placeholderText(isCLIInstalled: true, camOnline: false),
                       "Turn the camera on to see the live picture.")
    }

    func testDeviceNamesOnlyAcceptIndexedRowsAndDeduplicateNames() {
        let output = """
        Waiting for camera permission — click OK in the macOS dialog.
        0\tFaceTime HD Camera\t1280x720\t30
        not-a-device row
        1\tUSB Camera\t1920x1080\t60
        1\tUSB Camera\t1920x1080\t60
        """

        XCTAssertEqual(CameraFeedback.deviceNames(fromListOutput: output),
                       ["FaceTime HD Camera", "USB Camera"])
    }

    func testDeviceListPresentationKeepsSavedSelectionAndGuidanceHint() {
        XCTAssertEqual(
            CameraFeedback.deviceListPresentation(lines: [CameraFeedback.listFailed],
                                                  selectedDevice: ""),
            CameraDeviceListPresentation(devices: [],
                                         hint: CameraFeedback.listFailed)
        )

        XCTAssertEqual(
            CameraFeedback.deviceListPresentation(lines: ["USB Camera"],
                                                  selectedDevice: "Saved Camera"),
            CameraDeviceListPresentation(devices: ["Saved Camera", "USB Camera"],
                                         hint: "")
        )
    }
}
