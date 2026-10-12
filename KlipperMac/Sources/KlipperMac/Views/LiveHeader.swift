import SwiftUI

/// Compact live-status strip at the top of the Printer pane: klippy state,
/// WebSocket health, position, speed, save-config warning.
struct LiveHeader: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 10) {
            StateChip(text: klippyText, color: klippyColor)
            StateChip(text: model.wsConnected ? "ws" : "ws down",
                      color: model.wsConnected ? .green : .secondary)
            if let pos = model.live.gcodePosition {
                Text(String(format: "X %.1f  Y %.1f  Z %.2f", pos.x, pos.y, pos.z))
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let v = model.live.speedFactor {
                Text("speed \(Int((v * 100).rounded()))%")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if model.live.saveConfigPending {
                Label("SAVE_CONFIG pending", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 9)
        .background(.bar)
    }

    private var klippyText: String {
        switch model.live.klippy {
        case .ready: return "READY"
        case .startup, .connecting: return "STARTING"
        case .shutdown: return "SHUTDOWN"
        case .error: return "ERROR"
        case .disconnected: return "DISCONNECTED"
        case .unknown: return "…"
        }
    }

    private var klippyColor: Color {
        switch model.live.klippy {
        case .ready: return .green
        case .shutdown, .error: return .red
        case .startup, .connecting: return .orange
        case .disconnected, .unknown: return .secondary
        }
    }
}
