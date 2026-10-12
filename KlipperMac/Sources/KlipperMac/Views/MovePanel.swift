import SwiftUI

struct MovePanel: View {
    @EnvironmentObject private var model: AppModel

    private let steps: [Double] = [0.1, 1, 10, 100]
    @State private var step = 1.0
    @State private var filLength = "10"
    @State private var filSpeed = "5"
    @State private var speedPercent = 100.0
    @State private var draggingSpeed = false

    var body: some View {
        Panel("Movement", icon: "arrow.up.and.down.and.arrow.left.and.right", tint: .purple) {
            HStack {
                Button("Home All") { model.home("") }
                Button("X") { model.home("X") }
                Button("Y") { model.home("Y") }
                Button("Z") { model.home("Z") }
                Spacer()
                Picker("step", selection: $step) {
                    ForEach(steps, id: \.self) { Text("\($0) mm").tag($0) }
                }
                .pickerStyle(.menu)
                .fixedSize()
            }

            HStack(alignment: .center, spacing: 20) {
                jogPad
                VStack(alignment: .leading, spacing: 6) {
                    Text("Z")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack(spacing: 6) {
                        Button { model.zMove(step) } label: { Image(systemName: "arrow.up") }
                        Button { model.zMove(-step) } label: { Image(systemName: "arrow.down") }
                    }
                    Text("Filament")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack(spacing: 4) {
                        TextField("len", text: $filLength)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 48)
                        TextField("mm/s", text: $filSpeed)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 52)
                    }
                    HStack(spacing: 6) {
                        Button("Load") { moveFilament(1) }
                        Button("Unload") { moveFilament(-1) }
                    }
                }
            }

            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text("Speed")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("\(Int(speedPercent))%")
                        .font(.system(.caption, design: .monospaced))
                }
                Slider(value: $speedPercent, in: 25...150, step: 5,
                       onEditingChanged: { editing in
                    draggingSpeed = editing
                    if !editing { model.setSpeedFactor(speedPercent) }
                })
            }
        }
        .onReceive(model.$live) { live in
            // follow server reports unless the user is mid-drag
            guard !draggingSpeed, let f = live.speedFactor else { return }
            let pct = (f * 100).rounded()
            if abs(pct - speedPercent) > 1 { speedPercent = pct }
        }
    }

    private var jogPad: some View {
        VStack(spacing: 4) {
            jogButton("arrow.up", y: step)
            HStack(spacing: 4) {
                jogButton("arrow.left", x: -step)
                Button { model.home("") } label: { Image(systemName: "house.fill") }
                    .help("Home all")
                jogButton("arrow.right", x: step)
            }
            jogButton("arrow.down", y: -step)
        }
    }

    private func jogButton(_ symbol: String, x: Double = 0, y: Double = 0) -> some View {
        Button { model.jog(x: x, y: y) } label: {
            Image(systemName: symbol).frame(width: 28, height: 22)
        }
    }

    private func moveFilament(_ direction: Double) {
        let len = Double(filLength) ?? 10
        let speed = Double(filSpeed) ?? 5
        model.extrude(length: len * direction, speed: speed)
    }
}
