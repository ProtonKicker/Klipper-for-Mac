import SwiftUI

@main
struct KlipperForMacApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .task { await model.bootstrap() }
        }
        .windowStyle(.hiddenTitleBar)
        .windowToolbarStyle(.unifiedCompact)
        .defaultSize(width: 960, height: 660)
        .commands {
            CommandGroup(replacing: .help) {
                Button("Klipper for Mac on GitHub") {
                    if let url = URL(string: "https://github.com/ProtonKicker/Klipper-for-Mac") {
                        NSWorkspace.shared.open(url)
                    }
                }
            }
        }
    }
}
