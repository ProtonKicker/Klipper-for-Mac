import SwiftUI

@main
struct KlipperForMacApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .task { await model.bootstrap() }
        }
        .defaultSize(width: 880, height: 620)
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
