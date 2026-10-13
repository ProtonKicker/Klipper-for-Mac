import SwiftUI

/// Shared inputs for every section, so the panel reads as a list of section
/// names instead of a list of constructor arguments. Sections that only fire
/// one-shot actions take `model` by plain reference — they must not observe it,
/// or the 2s poll would re-render every row on the page.
@MainActor
struct PageInput {
    let model: AppModel
    let lanIP: String?
    let onRequestKill: () -> Void

    @ViewBuilder var setup: some View {
        if !model.env.installed {
            SetupBlock(model: model)
        }
    }

    var stack: some View { StackBlock(model: model, onRequestKill: onRequestKill) }

    @ViewBuilder var printer: some View {
        if let p = model.printer {
            PrinterBlock(model: model, printer: p)
        }
    }

    var links: some View { LinksBlock(model: model, lanIP: lanIP) }

    var config: some View { ConfigSectionBlock(model: model) }

    @ViewBuilder var components: some View {
        if model.env.installed {
            ComponentsBlock(model: model)
        }
    }

    var logs: some View { LogsBlock(model: model) }
}

/// The whole dashboard: one centered card, sections stacked vertically.
struct SinglePanelView: View {
    @ObservedObject var model: AppModel
    let lanIP: String?
    let onRequestKill: () -> Void

    static let githubURL = "https://github.com/ProtonKicker/Klipper-for-Mac"

    private var input: PageInput {
        PageInput(model: model, lanIP: lanIP, onRequestKill: onRequestKill)
    }

    var body: some View {
        PanelCard {
            PanelHeader(title: "Klipper for Mac",
                        state: stateWord,
                        overall: model.snapshot.overall)
            input.setup
            input.stack
            input.printer
            input.links
            input.config
            input.components
            input.logs
            PanelFooter(onOpenGitHub: { model.openURL(Self.githubURL) })
        }
        // Scoped here, not on the window root, so it never reaches the setup sheet.
        .controlSize(.small)
    }

    private var stateWord: String {
        switch model.snapshot.overall {
        case .running: return "Stack running"
        case .notResponding: return "Stack not responding"
        case .stopped: return "Stack stopped"
        }
    }
}
