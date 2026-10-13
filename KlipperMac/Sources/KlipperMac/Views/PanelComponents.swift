import AppKit
import SwiftUI

/// The single panel's shared chrome: card, sections, and the demo's custom
/// controls. Every view here reads colours exclusively through `Palette`/`Tint`
/// and renders the same in light and dark.

// MARK: - fonts

extension Font {
    static func panelMono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

enum AppInfo {
    static var version: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }
}

// MARK: - card

struct PanelCard<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            content()
        }
        .frame(maxWidth: 620)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14)
            .strokeBorder(Palette.borderSubtle, lineWidth: 1))
        .shadow(color: .black.opacity(0.04), radius: 1, y: 1)
        .shadow(color: .black.opacity(0.04), radius: 16, y: 4)
    }
}

struct Hairline: View {
    var body: some View {
        Rectangle()
            .fill(Palette.borderSubtle)
            .frame(height: 1)
    }
}

// MARK: - header

struct PanelHeader: View {
    let title: String
    let state: String
    let overall: StackSnapshot.Overall

    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 5) {
                Text(title)
                    .font(.system(size: 20, weight: .semibold))
                    .kerning(-0.5)
                    .foregroundStyle(Palette.textPrimary)
                HStack(spacing: 8) {
                    Text(state)
                        .fontWeight(.medium)
                        .foregroundStyle(Palette.textSecondary)
                    Circle()
                        .fill(Palette.textMuted)
                        .frame(width: 3, height: 3)
                    Text(AppInfo.version)
                        .font(.panelMono(11.5))
                        .kerning(-0.1)
                }
                .font(.system(size: 13))
                .foregroundStyle(Palette.textMuted)
            }
            Spacer(minLength: 12)
            StatusPill(overall: overall)
                .padding(.top, 2)
        }
        .padding(.horizontal, 28)
        .padding(.top, 28)
        .padding(.bottom, 24)
    }
}

// MARK: - section

struct PanelSection<Trailing: View, Content: View>: View {
    let label: String?
    @ViewBuilder var trailing: () -> Trailing
    @ViewBuilder var content: () -> Content

    init(_ label: String? = nil,
         @ViewBuilder trailing: @escaping () -> Trailing,
         @ViewBuilder content: @escaping () -> Content) {
        self.label = label
        self.trailing = trailing
        self.content = content
    }

    var body: some View {
        VStack(spacing: 0) {
            Hairline()
            VStack(alignment: .leading, spacing: 14) {
                if let label {
                    if Trailing.self == EmptyView.self {
                        SectionLabel(label)
                    } else {
                        HStack(spacing: 8) {
                            SectionLabel(label)
                            Spacer(minLength: 8)
                            trailing()
                        }
                    }
                }
                content()
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 22)
        }
    }
}

extension PanelSection where Trailing == EmptyView {
    init(_ label: String? = nil, @ViewBuilder content: @escaping () -> Content) {
        self.init(label, trailing: { EmptyView() }, content: content)
    }
}

struct SectionLabel: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10.5, weight: .semibold))
            .tracking(0.9)
            .foregroundStyle(Palette.textMuted)
    }
}

// MARK: - buttons

enum DemoButtonVariant {
    case bordered, primary, danger
}

struct DemoButton: ButtonStyle {
    var variant: DemoButtonVariant = .bordered
    var small = false

    func makeBody(configuration: Configuration) -> some View {
        Styled(configuration: configuration, variant: variant, small: small)
    }

    private struct Styled: View {
        @Environment(\.isEnabled) private var isEnabled
        let configuration: ButtonStyle.Configuration
        let variant: DemoButtonVariant
        let small: Bool
        @State private var hovering = false

        var body: some View {
            configuration.label
                .font(.system(size: small ? 12 : 13, weight: .medium))
                .foregroundStyle(fg)
                .padding(.horizontal, small ? 11 : 16)
                .frame(height: small ? 28 : 34)
                .background(bg)
                .overlay(RoundedRectangle(cornerRadius: small ? 4 : 6)
                    .strokeBorder(border, lineWidth: 1))
                .scaleEffect(configuration.isPressed ? 0.985 : 1)
                .opacity(isEnabled ? 1 : 0.45)
                .contentShape(RoundedRectangle(cornerRadius: small ? 4 : 6))
                .onHover { hovering = $0 }
                .animation(.easeOut(duration: 0.15), value: hovering)
        }

        private var fg: Color {
            switch variant {
            case .bordered: return Palette.textPrimary
            case .primary: return Palette.onAccent
            case .danger: return Palette.red
            }
        }
        private var bg: Color {
            switch variant {
            case .bordered: return hovering ? Palette.pageBG : Palette.surface
            case .primary: return hovering ? Palette.accentHover : Palette.accent
            case .danger: return hovering ? Palette.red.opacity(0.08) : Palette.surface
            }
        }
        private var border: Color {
            switch variant {
            case .bordered: return Palette.border
            case .primary: return bg
            case .danger: return Palette.red.opacity(0.35)
            }
        }
    }
}

// MARK: - segmented control

struct DemoSegmented: View {
    struct Item {
        let value: String
        let title: String
    }

    let items: [Item]
    let selected: String
    let select: (String) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(items, id: \.value) { item in
                Segment(item: item, active: item.value == selected, select: select)
            }
        }
        .padding(2.5)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(Palette.pageBG)
            .overlay(RoundedRectangle(cornerRadius: 6)
                .strokeBorder(Palette.border, lineWidth: 1)))
    }

    private struct Segment: View {
        let item: Item
        let active: Bool
        let select: (String) -> Void
        @State private var hovering = false

        var body: some View {
            Button {
                if !active { select(item.value) }
            } label: {
                Text(item.title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(active ? Palette.textPrimary
                                            : (hovering ? Palette.textSecondary : Palette.textMuted))
                    .padding(.horizontal, 16)
                    .padding(.vertical, 5)
                    .background {
                        if active {
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Palette.surface)
                                .shadow(color: .black.opacity(0.05), radius: 1, y: 1)
                        }
                    }
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onHover { hovering = $0 }
            .accessibilityLabel(item.title)
            .accessibilityAddTraits(active ? .isSelected : [])
        }
    }
}

// MARK: - switch

struct DemoSwitch: View {
    let isOn: Bool
    let toggle: () -> Void

    var body: some View {
        ZStack(alignment: .leading) {
            Capsule()
                .fill(isOn ? Palette.accent : Palette.border)
                .frame(width: 36, height: 21)
            Circle()
                .fill(Color.white)
                .frame(width: 15, height: 15)
                .shadow(color: .black.opacity(0.15), radius: 1, y: 1)
                .offset(x: isOn ? 18 : 3)
        }
        .frame(width: 36, height: 21, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture { toggle() }
        .accessibilityElement()
        .accessibilityLabel(isOn ? "On" : "Off")
        .accessibilityAddTraits(.isButton)
        .animation(.easeOut(duration: 0.2), value: isOn)
    }
}

// MARK: - rows

struct ConfigRow<Content: View>: View {
    let label: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        HStack(spacing: 12) {
            Text(label)
                .font(.system(size: 13))
                .foregroundStyle(Palette.textSecondary)
                .frame(width: 88, alignment: .leading)
            content()
        }
    }
}

struct KVRow<Value: View>: View {
    let key: String
    @ViewBuilder var value: () -> Value

    var body: some View {
        HStack(spacing: 12) {
            Text(key)
                .font(.system(size: 13))
                .foregroundStyle(Palette.textSecondary)
            Spacer(minLength: 12)
            value()
        }
    }
}

extension KVRow where Value == Text {
    init(_ key: String, _ value: String) {
        self.init(key: key, value: {
            Text(value)
                .font(.system(size: 12.5))
                .foregroundColor(Palette.textPrimary)
        })
    }
}

struct PathChip: View {
    let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(.panelMono(11.5))
            .foregroundStyle(Palette.textSecondary)
            .lineLimit(1)
            .truncationMode(.middle)
            .textSelection(.enabled)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 4)
                .fill(Palette.pageBG)
                .overlay(RoundedRectangle(cornerRadius: 4)
                    .strokeBorder(Palette.border, lineWidth: 1)))
    }
}

// MARK: - menu select

struct MenuSelectOption {
    let value: String
    let title: String
}

struct MenuSelect: View {
    let placeholder: String
    let options: [MenuSelectOption]
    let disabled: Bool
    let select: (String) -> Void

    var body: some View {
        Menu {
            ForEach(options, id: \.value) { option in
                Button(option.title) { select(option.value) }
            }
        } label: {
            HStack(spacing: 6) {
                Text(placeholder)
                    .font(.panelMono(11.5))
                    .foregroundStyle(disabled ? Palette.textMuted : Palette.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 8)
                Image(systemName: "chevron.down")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(Palette.textMuted)
            }
            .padding(.horizontal, 10)
            .frame(height: 28)
            .frame(maxWidth: .infinity)
            .background(RoundedRectangle(cornerRadius: 4)
                .fill(Palette.surface)
                .overlay(RoundedRectangle(cornerRadius: 4)
                    .strokeBorder(Palette.border, lineWidth: 1)))
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize(horizontal: false, vertical: true)
        .disabled(disabled)
    }
}

// MARK: - list rows

struct PanelListRow<Name: View, Detail: View>: View {
    @ViewBuilder var name: () -> Name
    @ViewBuilder var detail: () -> Detail

    var body: some View {
        HStack(spacing: 12) {
            name()
                .frame(maxWidth: .infinity, alignment: .leading)
            detail()
                .foregroundStyle(Palette.textSecondary)
                .font(.panelMono(11.5))
        }
        .padding(.vertical, 11)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - footer

struct PanelFooter: View {
    let onOpenGitHub: () -> Void

    var body: some View {
        HStack {
            Button(action: onOpenGitHub) {
                Text("Made with Klipper for Mac")
                    .underline(hovered, pattern: .solid, color: Palette.textSecondary)
                    .font(.system(size: 12))
                    .foregroundStyle(hovered ? Palette.textSecondary : Palette.textMuted)
            }
            .buttonStyle(.plain)
            .onHover { hovered = $0 }
            .help("Open the project page on GitHub")

            Spacer()

            Text(AppInfo.version)
                .font(.panelMono(11))
                .foregroundStyle(Palette.textMuted)
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 18)
        .background { Hairline().frame(maxHeight: .infinity, alignment: .top) }
    }

    @State private var hovered = false
}
