import SwiftUI

/// The entire app: one flat white page, no scrolling. A Notion-style page
/// header (icon + large title + actions), then two columns of database
/// blocks, and a slim footer.
struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.top, 14)
                .padding(.bottom, 10)
            if model.env.installed {
                columns
            } else {
                InstallBlock(model: model)
            }
            Spacer(minLength: 12)
            footer
        }
        .frame(maxWidth: 1000)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.horizontal, 32)
        .padding(.bottom, 10)
        .frame(minWidth: 1040, minHeight: 560)
        .background(Skin.page.ignoresSafeArea())
        // ⌘R stays available without a visible button — the page polls anyway.
        .background(
            Button("") { Task { await model.refresh() } }
                .keyboardShortcut("r", modifiers: .command)
                .opacity(0)
        )
        .sheet(isPresented: $model.showSheet) {
            StreamSheet(title: model.sheetTitle, lines: model.sheetLines,
                        running: model.sheetRunning)
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 12) {
            // clearance for the traffic lights (hidden title bar)
            Color.clear.frame(width: 56, height: 1)
            Image(systemName: "printer.fill")
                .font(.system(size: 24))
                .foregroundStyle(Skin.accent)
            Text("Klipper for Mac")
                .font(.system(size: 28, weight: .bold))
                .kerning(-0.8)
                .foregroundStyle(Skin.ink)
            StatusChip(overall: model.snapshot.overall)
                .padding(.leading, 6)
            if model.busy {
                ProgressView().controlSize(.mini)
            }
            Spacer(minLength: 16)
            controls
        }
    }

    private var controls: some View {
        let running = model.snapshot.overall != .stopped
        return HStack(spacing: 8) {
            if running {
                Button {
                    Task { await model.stopStack() }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "stop.fill").font(.system(size: 10))
                        Text("Stop")
                    }
                }
                .buttonStyle(NotionButton())
                .disabled(model.busy)
                .keyboardShortcut("s", modifiers: .command)
            } else {
                Button {
                    Task { await model.startStack() }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "play.fill").font(.system(size: 10))
                        Text("Start")
                    }
                }
                .buttonStyle(NotionButton(variant: .blue))
                .disabled(model.busy || !model.env.installed)
                .keyboardShortcut("s", modifiers: .command)
            }

            Button {
                Task { await model.restartStack() }
            } label: {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.system(size: 12, weight: .medium))
            }
            .buttonStyle(NotionButton())
            .disabled(model.busy || !model.env.installed)
            .help("Restart the stack")

            Button {
                Task { await model.killAll() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.octagon.fill").font(.system(size: 11))
                    Text("Kill")
                }
            }
            .buttonStyle(NotionButton(variant: .red))
            .disabled(model.busy || !model.snapshot.tracked)
            .help("Force kill everything, including strays a normal stop would miss")
        }
    }

    // MARK: columns

    /// Two Notion columns of database blocks — services on the left,
    /// software and settings on the right.
    private var columns: some View {
        HStack(alignment: .top, spacing: 48) {
            VStack(alignment: .leading, spacing: 28) {
                StackTable(model: model)
                PresetsTable(model: model)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            VStack(alignment: .leading, spacing: 28) {
                UpdatesTable(model: model)
                PreferencesTable(model: model)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }

    // MARK: footer

    private var footer: some View {
        HStack(spacing: 0) {
            Text(AppInfo.version)
                .font(.mono(10.5))
                .foregroundStyle(Skin.inkMuted)
            Spacer()
            FooterLink(text: model.env.uiName == "mainsail" ? "Mainsail" : "Fluidd",
                       url: model.env.webURL, live: model.snapshot.anyUp, model: model)
        }
        .padding(.horizontal, 8)
    }
}

private struct FooterLink: View {
    let text: String
    let url: String
    let live: Bool
    let model: AppModel
    @State private var hovering = false

    var body: some View {
        Button {
            model.openURL(url)
        } label: {
            Text(text)
                .font(.system(size: 11.5))
                .foregroundStyle(hovering ? Skin.accent : Skin.inkMuted)
                .underline(hovering, pattern: .solid, color: Skin.accent.opacity(0.5))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .opacity(live ? 1 : 0.5)
        .help(url)
    }
}

// MARK: - first-run install

struct InstallBlock: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "printer")
                .font(.system(size: 36))
                .foregroundStyle(Skin.accent)
            Text("Install the Klipper stack")
                .font(.system(size: 20, weight: .bold))
                .kerning(-0.4)
                .foregroundStyle(Skin.ink)
            Text("Fetches Klipper, Moonraker and the web UIs from GitHub.\nTakes a few minutes and needs internet.")
                .font(.system(size: 13))
                .foregroundStyle(Skin.inkSecondary)
                .multilineTextAlignment(.center)
            Button {
                Task { await model.runSetup() }
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: "play.fill").font(.system(size: 10))
                    Text(model.busy ? "Working…" : "Install")
                }
            }
            .buttonStyle(NotionButton(variant: .blue))
            .disabled(model.busy)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - streaming sheet (setup / updates)

struct StreamSheet: View {
    let title: String
    let lines: [String]
    let running: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(title)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Skin.ink)
                Spacer()
                if running { ProgressView().controlSize(.small) }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { i, l in
                            Text(l)
                                .font(.mono(11))
                                .foregroundStyle(Skin.inkSecondary)
                                .id(i)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                }
                .background(RoundedRectangle(cornerRadius: 6).fill(Skin.well))
                .overlay(RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(Skin.tableLine, lineWidth: 1))
                .onChange(of: lines.count) { _ in
                    withAnimation(.easeOut(duration: 0.15)) {
                        proxy.scrollTo(max(lines.count - 1, 0), anchor: .bottom)
                    }
                }
            }
        }
        .padding(16)
        .frame(width: 560, height: 380)
    }
}
