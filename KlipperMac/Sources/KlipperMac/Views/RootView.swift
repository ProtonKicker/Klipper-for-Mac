import SwiftUI

/// The entire app: one page, no scrolling. Header chrome (title, status,
/// stack controls), a bento grid that fills the window, and a slim footer.
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showKillConfirm = false

    var body: some View {
        VStack(spacing: 16) {
            header
            if model.env.installed {
                bento
            } else {
                InstallCard(model: model)
            }
            footer
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 12)
        .frame(minWidth: 860, minHeight: 560)
        .background(
            LinearGradient(colors: [Skin.washTop, Skin.washBottom],
                           startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
        )
        .overlay(alignment: .bottom) { Toast(notifier: model.notifier) }
        .sheet(isPresented: $model.showSheet) {
            StreamSheet(title: model.sheetTitle, lines: model.sheetLines,
                        running: model.sheetRunning)
        }
        .confirmationDialog("Force kill everything, including strays a normal stop would miss?",
                            isPresented: $showKillConfirm, titleVisibility: .visible) {
            Button("Kill everything", role: .destructive) {
                Task { await model.killAll() }
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 12) {
            // clearance for the traffic lights (hidden title bar)
            Color.clear.frame(width: 56, height: 1)
            VStack(alignment: .leading, spacing: 2) {
                Text("Klipper for Mac")
                    .font(.system(size: 24, weight: .bold))
                    .kerning(-0.6)
                    .foregroundStyle(Skin.ink)
            }
            StatusPill(overall: model.snapshot.overall)
                .padding(.leading, 4)
            if model.busy {
                ProgressView().controlSize(.small)
                    .padding(.leading, 2)
            }
            Spacer(minLength: 16)
            controls
        }
    }

    @ViewBuilder private var controls: some View {
        let running = model.snapshot.overall == .running
        HStack(spacing: 8) {
            if !running {
                Button {
                    Task { await model.startStack() }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "play.fill").font(.system(size: 10))
                        Text("Start")
                    }
                }
                .buttonStyle(AppleButton(variant: .filled))
                .disabled(model.busy || !model.env.installed)
                .keyboardShortcut("s", modifiers: .command)
            } else {
                Button {
                    Task { await model.stopStack() }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "stop.fill").font(.system(size: 10))
                        Text("Stop")
                    }
                }
                .buttonStyle(AppleButton())
                .disabled(model.busy)
                .keyboardShortcut("s", modifiers: .command)
            }

            Button {
                Task { await model.restartStack() }
            } label: {
                Image(systemName: "arrow.triangle.2.circlepath")
            }
            .buttonStyle(AppleButton())
            .disabled(model.busy || !model.env.installed)
            .help("Restart the stack")

            IconButton(symbol: "arrow.clockwise", help: "Refresh (⌘R)") {
                Task { await model.refresh() }
            }
            .keyboardShortcut("r", modifiers: .command)

            Menu {
                Button("Kill everything…", role: .destructive) { showKillConfirm = true }
                Divider()
                Button("Open web UI") { model.openURL(model.env.webURL) }
                Button("Open Moonraker API") { model.openURL(model.env.moonrakerURL) }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Skin.inkSecondary)
                    .frame(width: 28, height: 28)
                    .background(Circle().fill(Skin.card))
                    .overlay(Circle().strokeBorder(Skin.hairline, lineWidth: 1))
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("More")
        }
    }

    // MARK: bento

    private var bento: some View {
        GeometryReader { g in
            let gap: CGFloat = 16
            let mainW = max(360, (g.size.width - gap) * 0.56)
            VStack(spacing: gap) {
                HStack(spacing: gap) {
                    HeroCard(model: model)
                        .frame(width: mainW)
                    VStack(spacing: gap) {
                        FilamentCard(printer: model.printer)
                        CameraCard(webcam: model.webcam,
                                   onOpen: { model.openURL($0) })
                    }
                    .frame(maxWidth: .infinity)
                }
                .frame(height: g.size.height * 0.52)

                HStack(spacing: gap) {
                    TempCard(history: model.tempHistory, printer: model.printer)
                        .frame(width: mainW)
                    ConsoleCard(model: model, live: model.snapshot.anyUp)
                        .frame(maxWidth: .infinity)
                }
                .frame(maxHeight: .infinity)
            }
        }
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
            Text("  ·  ").foregroundStyle(Skin.inkMuted)
            FooterLink(text: "Moonraker", url: model.env.moonrakerURL,
                 live: model.snapshot.anyUp, model: model)
        }
        .padding(.horizontal, 4)
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

struct InstallCard: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GlassCard {
            VStack(spacing: 14) {
                Image(systemName: "arrow.down.app")
                    .font(.system(size: 34, weight: .thin))
                    .foregroundStyle(Skin.accent)
                Text("Install the Klipper stack")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Skin.ink)
                Text("Fetches Klipper, Moonraker and the web UIs from GitHub.\nTakes a few minutes and needs internet.")
                    .font(.system(size: 12.5))
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
                .buttonStyle(AppleButton(variant: .filled))
                .disabled(model.busy)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
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
                .background(Skin.well, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Skin.hairline, lineWidth: 1))
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
