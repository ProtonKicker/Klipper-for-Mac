import AppKit
import SwiftUI

/// AppKit entry point: the SwiftUI `App` lifecycle (WindowGroup, commands,
/// Settings scene) requires macOS 11+, so the app hosts its SwiftUI views in
/// an NSHostingView — the one lifecycle that works on macOS 10.15 through
/// current releases with a single binary.
@main
struct KlipperForMacMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        buildMenu()
        app.run()
    }

    private static func buildMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        appMenuItem.submenu = NSMenu()
        let appName = ProcessInfo.processInfo.processName
        appMenuItem.submenu?.items = [
            NSMenuItem(title: "About \(appName)",
                       action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                       keyEquivalent: ""),
            .separator(),
            NSMenuItem(title: "Hide \(appName)",
                       action: #selector(NSApplication.hide(_:)),
                       keyEquivalent: "h"),
            .separator(),
            NSMenuItem(title: "Quit \(appName)",
                       action: #selector(NSApplication.terminate(_:)),
                       keyEquivalent: "q"),
        ]
        mainMenu.addItem(appMenuItem)

        let windowMenuItem = NSMenuItem()
        windowMenuItem.submenu = NSMenu()
        windowMenuItem.submenu?.items = [
            NSMenuItem(title: "Minimize",
                       action: #selector(NSWindow.performMiniaturize(_:)),
                       keyEquivalent: "m"),
            NSMenuItem(title: "Close Window",
                       action: #selector(NSWindow.performClose(_:)),
                       keyEquivalent: "w"),
        ]
        mainMenu.addItem(windowMenuItem)
        NSApp.mainMenu = mainMenu
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow?
    private var model: AppModel?
    /// Set when an in-app update is staged and the app should restart to apply it.
    var allowTerminateForUpdate = false

    nonisolated func applicationDidFinishLaunching(_ notification: Notification) {
        // Delegate callbacks are non-isolated but always run on the main
        // thread. Hop onto the main actor once to build the model + window.
        Task { @MainActor in
            let model = AppModel()
            self.model = model

            let root = ContentView()
                .environmentObject(model)

            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 880, height: 620),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            window.title = "Klipper for Mac"
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.contentView = NSHostingView(rootView: root)
            window.setContentSize(NSSize(width: 880, height: 620))
            window.center()
            window.setFrameAutosaveName("KlipperMainWindow")
            window.makeKeyAndOrderFront(nil)
            self.window = window

            NSApp.activate(ignoringOtherApps: true)

            // Kick off the first poll + liveness observers once the view tree exists.
            await model.bootstrap()
        }
    }

    nonisolated func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // Keep the process alive when the UI is hidden and services are running.
        false
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Closing the window hides the UI while the stack runs; quit only when stopped.
        if StackReader.readSync(env: KEnvironment.shared).anyUp {
            sender.orderOut(nil)
            NSApp.setActivationPolicy(.accessory)
            return false
        }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let stackRunning = StackReader.readSync(env: KEnvironment.shared).anyUp
        // A staged app update should relaunch the app; only allow it when the stack is not running.
        if allowTerminateForUpdate && !stackRunning {
            NSApp.setActivationPolicy(.regular)
            return .terminateNow
        }
        // If a Klipper instance/service is running, hide UI but keep services on.
        // Fully quit only when nothing is running.
        if stackRunning {
            if let w = window ?? NSApp.windows.first {
                w.orderOut(nil)
            }
            NSApp.setActivationPolicy(.accessory)
            return .terminateCancel
        }
        NSApp.setActivationPolicy(.regular)
        return .terminateNow
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            NSApp.setActivationPolicy(.regular)
            window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        return true
    }
}