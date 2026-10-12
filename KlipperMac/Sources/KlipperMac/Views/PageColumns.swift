import SwiftUI

/// Shared inputs for every block, so an arrangement reads as a list of block names
/// instead of a list of constructor arguments. Blocks take `model` by plain reference —
/// they must not observe it, or the 2s poll would re-render every row on the page.
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

    var access: some View { AccessBlock(model: model, lanIP: lanIP) }

    @ViewBuilder var config: some View {
        if model.env.installed {
            ConfigBlock(model: model)
        }
    }

    var about: some View { AboutBlock(model: model) }
    var data: some View { DataBlock(model: model) }
    var updates: some View { UpdatesBlock(model: model) }
    var logs: some View { LogsBlock(model: model) }
}

extension View {
    /// One column of the page: a real grouped Form, so it keeps the native inset-card
    /// chrome, bold section headers and hairline separators rather than imitating them.
    /// `scrollDisabled` hands scrolling to the page's outer ScrollView; without it each
    /// column fills the viewport and scrolls on its own.
    func pageColumn() -> some View {
        formStyle(.grouped)
            .frame(maxWidth: .infinity)
            .scrollDisabled(true)
    }
}

/// The column set for the current width. Every arrangement lists every block that is
/// eligible at that width — dropping a column must never drop its content.
///
/// Three columns require a live printer: without it the Printer card and the service
/// band are both absent, and a third column would hold one short card above a large
/// void. Idle content splits evenly across two instead.
struct PageBody: View {
    let width: CGFloat
    @ObservedObject var model: AppModel
    let lanIP: String?
    let onRequestKill: () -> Void

    private var input: PageInput {
        PageInput(model: model, lanIP: lanIP, onRequestKill: onRequestKill)
    }

    var body: some View {
        let live = model.printer != nil
        HStack(alignment: .top, spacing: 0) {
            if live && width >= 1060 {
                Form {
                    input.setup
                    input.stack
                    input.printer
                }.pageColumn()
                Form {
                    input.access
                    input.config
                    input.updates
                }.pageColumn()
                Form {
                    input.about
                    input.data
                    input.logs
                }.pageColumn()
            } else if width >= 780 {
                if live {
                    Form {
                        input.setup
                        input.stack
                        input.printer
                        input.access
                    }.pageColumn()
                    Form {
                        input.config
                        input.about
                        input.data
                        input.updates
                        input.logs
                    }.pageColumn()
                } else {
                    Form {
                        input.setup
                        input.stack
                        input.access
                        input.config
                    }.pageColumn()
                    Form {
                        input.about
                        input.data
                        input.updates
                        input.logs
                    }.pageColumn()
                }
            } else {
                Form {
                    input.setup
                    input.stack
                    input.printer
                    input.access
                    input.config
                    input.about
                    input.data
                    input.updates
                    input.logs
                }.pageColumn()
            }
        }
        // Scoped here, not on the page root, so it never reaches the setup sheet.
        .controlSize(.small)
        .frame(maxWidth: 1440)
        .frame(maxWidth: .infinity)
    }
}
