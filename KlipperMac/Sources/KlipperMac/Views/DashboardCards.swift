import SwiftUI

// MARK: - hero: print progress + ETA

struct HeroCard: View {
    @ObservedObject var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let p = model.printer
        let active = p?.printState == "printing" || p?.printState == "paused"
        let prog = active ? min(max(p?.progress ?? 0, 0), 1) : 0

        GlassCard {
            HStack(spacing: 20) {
                ZStack {
                    Circle().stroke(Skin.trackFill, lineWidth: 6)
                    Circle()
                        .trim(from: 0, to: CGFloat(prog))
                        .stroke(Skin.accent, style: StrokeStyle(lineWidth: 6, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 72, height: 72)
                .animation(reduceMotion ? nil : .spring(response: 0.5, dampingFraction: 0.8),
                           value: prog)

                VStack(alignment: .leading, spacing: 3) {
                    Text(stateWord(p))
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Skin.inkSecondary)
                    HStack(alignment: .firstTextBaseline, spacing: 1) {
                        if p == nil {
                            Text("—").font(.system(size: 46, weight: .thin))
                                .foregroundStyle(Skin.inkMuted)
                        } else {
                            Text("\(Int((prog * 100).rounded()))")
                                .font(.system(size: 46, weight: .thin))
                                .monospacedDigit()
                                .foregroundStyle(Skin.ink)
                            Text("%")
                                .font(.system(size: 22, weight: .light))
                                .foregroundStyle(Skin.inkSecondary)
                        }
                    }
                    .contentTransition(.numericText())
                    .animation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.85),
                               value: prog)
                    subline(p, active: active)
                }

                Spacer(minLength: 8)

                if active {
                    VStack(spacing: 8) {
                        if p?.printState == "printing" {
                            Button("Pause") { Task { await model.pausePrint() } }
                                .buttonStyle(AppleButton())
                        } else {
                            Button("Resume") { Task { await model.resumePrint() } }
                                .buttonStyle(AppleButton(variant: .filled))
                        }
                        Button("Cancel") { Task { await model.cancelPrint() } }
                            .buttonStyle(AppleButton(variant: .danger))
                    }
                    .fixedSize()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder
    private func subline(_ p: PrinterStatus?, active: Bool) -> some View {
        if active, let eta = model.etaSeconds {
            HStack(spacing: 5) {
                Image(systemName: "clock")
                    .font(.system(size: 10))
                    .foregroundStyle(Skin.inkMuted)
                Text("about \(Fmt.remaining(eta)) left · finishes \(Fmt.clock(Date().addingTimeInterval(eta)))")
                    .font(.system(size: 12.5))
                    .foregroundStyle(Skin.inkSecondary)
            }
        } else if let file = p?.filename, !file.isEmpty {
            Text(file)
                .font(.system(size: 12.5))
                .foregroundStyle(Skin.inkSecondary)
                .lineLimit(1)
                .truncationMode(.middle)
        } else {
            Text(idleLabel(p))
                .font(.system(size: 12.5))
                .foregroundStyle(Skin.inkSecondary)
                .lineLimit(1)
        }
    }

    private func stateWord(_ p: PrinterStatus?) -> String {
        switch p?.printState {
        case "printing": return "Printing"
        case "paused": return "Paused"
        case "complete": return "Complete"
        default:
            if p == nil { return "Offline" }
            return p?.connected == true ? "Ready" : "Connecting"
        }
    }

    private func idleLabel(_ p: PrinterStatus?) -> String {
        if p == nil { return "Start the stack to see live status" }
        if p?.connected != true { return "Klipper is \(p?.klippyState ?? "offline")" }
        if let m = p?.message, !m.isEmpty { return m }
        return "No active print"
    }
}

// MARK: - temperatures

struct TempCard: View {
    let history: [TempSample]
    let printer: PrinterStatus?

    var body: some View {
        GlassCard {
            HStack(spacing: 14) {
                MicroLabel("Temperatures")
                Spacer()
                HStack(spacing: 12) {
                    LegendSwatch(color: Skin.accent, text: "Nozzle")
                    LegendSwatch(color: Skin.inkMuted, text: "Bed")
                }
            }
            if history.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "thermometer.medium")
                        .font(.system(size: 18, weight: .light))
                        .foregroundStyle(Skin.inkMuted)
                    Text("Readings appear once the stack is running")
                        .font(.system(size: 12))
                        .foregroundStyle(Skin.inkMuted)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                HStack(alignment: .top, spacing: 32) {
                    if let h = printer?.hotend {
                        reading(value: h.temp, target: h.target, title: "Nozzle",
                                tint: Skin.accent)
                    }
                    if let b = printer?.bed {
                        reading(value: b.temp, target: b.target, title: "Bed",
                                tint: Skin.inkMuted)
                    }
                    Spacer(minLength: 0)
                }
                TempChart(history: history)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func reading(value: Double, target: Double, title: String,
                         tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Skin.inkSecondary)
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text("\(Int(value.rounded()))°")
                    .font(.system(size: 34, weight: .thin))
                    .monospacedDigit()
                    .foregroundStyle(Skin.ink)
                if target > 0 {
                    Text("→ \(Int(target.rounded()))°")
                        .font(.system(size: 12))
                        .foregroundStyle(tint.opacity(0.85))
                }
            }
            .contentTransition(.numericText())
        }
    }
}

private struct LegendSwatch: View {
    let color: Color
    let text: String
    var body: some View {
        HStack(spacing: 5) {
            Capsule().fill(color).frame(width: 8, height: 3)
            Text(text).font(.system(size: 10.5)).foregroundStyle(Skin.inkSecondary)
        }
    }
}

/// Last ~5 minutes as two curves; targets as faint dashed lines.
struct TempChart: View {
    let history: [TempSample]

    var body: some View {
        GeometryReader { proxy in
            let samples = Array(history.suffix(150))
            let maxY = max(50, samples.map {
                max($0.hotend, $0.hotendTarget, $0.bed, $0.bedTarget)
            }.max() ?? 50) * 1.08
            let w = proxy.size.width
            let h = proxy.size.height
            // Closures, not local funcs: function declarations inside a
            // ViewBuilder body get result-builder-transformed.
            let y: (Double) -> CGFloat = { v in h * (1 - CGFloat(v / maxY)) }
            let point: (Int, Double) -> CGPoint = { i, v in
                let n = max(samples.count - 1, 1)
                return CGPoint(x: w * CGFloat(i) / CGFloat(n), y: y(v))
            }
            let curve: (KeyPath<TempSample, Double>) -> Path = { key in
                var p = Path()
                guard samples.count > 1 else { return p }
                p.move(to: point(0, samples[0][keyPath: key]))
                for i in 1..<samples.count { p.addLine(to: point(i, samples[i][keyPath: key])) }
                return p
            }
            let targetLine: (Double) -> Path = { v in
                var p = Path()
                guard v > 0 else { return p }
                p.move(to: CGPoint(x: 0, y: y(v)))
                p.addLine(to: CGPoint(x: w, y: y(v)))
                return p
            }

            ZStack {
                ForEach([0.25, 0.5, 0.75], id: \.self) { f in
                    Rectangle()
                        .fill(Skin.gridLine)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                        .frame(height: 1)
                        .offset(y: h * CGFloat(f))
                }
                if let last = samples.last {
                    if last.hotendTarget > 0 {
                        targetLine(last.hotendTarget).stroke(
                            Skin.accent.opacity(0.30),
                            style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                    }
                    if last.bedTarget > 0 {
                        targetLine(last.bedTarget).stroke(
                            Skin.inkMuted.opacity(0.5),
                            style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                    }
                }
                curve(\.bed).stroke(Skin.inkMuted, lineWidth: 1.5)
                curve(\.hotend).stroke(Skin.accent,
                                        style: StrokeStyle(lineWidth: 2, lineJoin: .round))
            }
        }
    }
}

// MARK: - filament + head position

struct FilamentCard: View {
    let printer: PrinterStatus?

    var body: some View {
        GlassCard {
            MicroLabel("Filament")
            HStack(spacing: 7) {
                Circle().fill(statusColor).frame(width: 7, height: 7)
                Text(statusText)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(statusColor == Skin.inkMuted ? Skin.inkSecondary : statusColor)
                Spacer(minLength: 0)
            }
            Spacer(minLength: 0)
            if let pos = printer?.position {
                HStack(spacing: 16) {
                    axis("X", pos.x)
                    axis("Y", pos.y)
                    axis("Z", pos.z)
                }
            }
        }
    }

    private var statusColor: Color {
        switch printer?.filamentPresent {
        case .some(true): return Skin.ok
        case .some(false): return Skin.alert
        case nil: return Skin.inkMuted
        }
    }

    private var statusText: String {
        switch printer?.filamentPresent {
        case .some(true): return "Filament present"
        case .some(false): return "Filament out"
        case nil: return printer == nil ? "Waiting for the stack" : "No sensor configured"
        }
    }

    private func axis(_ name: String, _ v: Double) -> some View {
        HStack(spacing: 4) {
            Text(name)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Skin.inkMuted)
            Text(String(format: v >= 100 ? "%.0f" : "%.1f", v))
                .font(.system(size: 14, weight: .light))
                .monospacedDigit()
                .foregroundStyle(Skin.ink)
        }
    }
}

// MARK: - camera

struct CameraCard: View {
    let webcam: WebcamInfo?
    let onOpen: (String) -> Void

    var body: some View {
        GlassCard {
            HStack {
                MicroLabel("Camera")
                Spacer()
                if let cam = webcam {
                    Button("Open stream") { onOpen(cam.url) }
                        .buttonStyle(AppleButton(small: true))
                }
            }
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(.regularMaterial)
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Skin.hairline, lineWidth: 1)
                VStack(spacing: 6) {
                    Image(systemName: webcam == nil ? "video.slash" : "video")
                        .font(.system(size: 18, weight: .light))
                        .foregroundStyle(Skin.inkMuted)
                    Text(webcam.map(\.name) ?? "Camera not configured")
                        .font(.system(size: 11.5))
                        .foregroundStyle(Skin.inkMuted)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: - gcode console

struct ConsoleCard: View {
    @ObservedObject var model: AppModel
    let live: Bool
    @State private var command = ""

    var body: some View {
        GlassCard {
            HStack(spacing: 10) {
                MicroLabel("Console")
                Spacer()
                if !live {
                    Text("Goes live when the stack is running")
                        .font(.system(size: 10.5))
                        .foregroundStyle(Skin.inkMuted)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 3) {
                        if model.gcodeLog.isEmpty {
                            Text(live ? "No console output yet" : "—")
                                .font(.mono(11))
                                .foregroundStyle(Skin.inkMuted)
                                .frame(maxWidth: .infinity, alignment: .center)
                                .padding(.vertical, 40)
                        }
                        ForEach(model.gcodeLog.suffix(80)) { line in
                            HStack(alignment: .top, spacing: 6) {
                                if line.echoed {
                                    Image(systemName: "chevron.right")
                                        .font(.system(size: 8, weight: .semibold))
                                        .foregroundStyle(Skin.accent)
                                        .padding(.top, 3)
                                }
                                Text(line.message)
                                    .font(.mono(11))
                                    .foregroundStyle(line.echoed ? Skin.ink : Skin.inkSecondary)
                                    .textSelection(.enabled)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.id)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Skin.well, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Skin.hairlineSoft, lineWidth: 1))
                .onChange(of: model.gcodeLog.count) { _ in
                    if let last = model.gcodeLog.last {
                        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(last.id) }
                    }
                }
            }
            HStack(spacing: 8) {
                TextField("Send a G-code command…", text: $command)
                    .textFieldStyle(.plain)
                    .font(.mono(11.5))
                    .padding(.horizontal, 10)
                    .frame(height: 28)
                    .background(Skin.well, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Skin.hairline, lineWidth: 1))
                    .disabled(!live)
                    .onSubmit(send)
                Button(action: send) {
                    Image(systemName: "paperplane.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(Skin.ink)
                }
                .buttonStyle(AppleButton(small: true))
                .disabled(!live || command.trimmingCharacters(in: .whitespaces).isEmpty)
                .help("Send")
            }
        }
    }

    private func send() {
        let cmd = command
        command = ""
        Task { await model.sendGcode(cmd) }
    }
}
