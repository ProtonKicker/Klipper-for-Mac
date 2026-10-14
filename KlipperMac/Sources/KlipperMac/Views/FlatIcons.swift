import SwiftUI

/// Flat, outline-free icons drawn as vector paths, in Notion's muted palette.
enum FlatPalette {
    static let brown  = Color(red: 0.545, green: 0.369, blue: 0.290)
    static let red    = Color(red: 0.769, green: 0.333, blue: 0.239)
    static let amber  = Color(red: 0.722, green: 0.525, blue: 0.169)
    static let blue   = Color(red: 0.290, green: 0.482, blue: 0.659)
    static let green  = Color(red: 0.306, green: 0.541, blue: 0.373)
    static let purple = Color(red: 0.482, green: 0.369, blue: 0.655)
}

enum FlatIconKind {
    case grid, moon, gear, warning, download, link, info, check, close, camera
}

struct FlatIcon: View {
    let kind: FlatIconKind
    var size: CGFloat = 16
    var color: Color = FlatPalette.brown

    private var soft: Color { color.opacity(0.28) }
    private var stroke: StrokeStyle {
        StrokeStyle(lineWidth: max(1.2, size * 0.14), lineCap: .round, lineJoin: .round)
    }

    var body: some View {
        ZStack {
            switch kind {
            case .grid:
                GridTilesA().fill(soft)
                GridTilesB().fill(color)

            case .moon:
                MoonSatellite().fill(soft)
                MoonBody().fill(color)

            case .gear:
                GearHub().fill(soft)
                GearBody().fill(color, style: FillStyle(eoFill: true))

            case .warning:
                WarningGlyph().fill(color, style: FillStyle(eoFill: true))

            case .download:
                TrayGlyph().fill(soft)
                ArrowShaft().fill(color)
                ArrowHead().fill(color)

            case .link:
                LinkRing(top: true).fill(soft, style: FillStyle(eoFill: true))
                LinkRing(top: false).fill(color, style: FillStyle(eoFill: true))

            case .info:
                Circle().fill(soft)
                InfoGlyph().fill(color, style: FillStyle(eoFill: true))

            case .check:
                Circle().fill(soft)
                CheckGlyph().stroke(color, style: stroke)

            case .close:
                Circle().fill(soft)
                CloseGlyph().stroke(color, style: stroke)

            case .camera:
                CameraLens().fill(soft)
                CameraBody().fill(color, style: FillStyle(eoFill: true))
                CameraBump().fill(color)
            }
        }
        .frame(width: size, height: size)
        .fixedSize()
    }
}

// MARK: - 24pt design grid

private struct IconGrid {
    let rect: CGRect
    init(_ rect: CGRect) { self.rect = rect }

    var s: CGFloat { min(rect.width, rect.height) / 24 }
    var ox: CGFloat { rect.midX - 12 * s }
    var oy: CGFloat { rect.midY - 12 * s }

    func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        CGPoint(x: ox + x * s, y: oy + y * s)
    }
    func r(_ v: CGFloat) -> CGFloat { v * s }
    func box(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> CGRect {
        CGRect(x: ox + x * s, y: oy + y * s, width: w * s, height: h * s)
    }
    func dot(_ cx: CGFloat, _ cy: CGFloat, _ rad: CGFloat) -> CGRect {
        box(cx - rad, cy - rad, rad * 2, rad * 2)
    }
    func ring(_ cx: CGFloat, _ cy: CGFloat, _ rad: CGFloat) -> Path {
        Path(ellipseIn: dot(cx, cy, rad))
    }
    func capsule(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> Path {
        Path(roundedRect: box(x, y, w, h), cornerSize: CGSize(width: h / 2, height: h / 2))
    }
    func tile(_ x: CGFloat, _ y: CGFloat, _ side: CGFloat = 8.2) -> Path {
        Path(roundedRect: box(x, y, side, side),
             cornerSize: CGSize(width: r(2.2), height: r(2.2)))
    }
}

/// Triangle / polygon with softened corners, matching Notion's rounded silhouettes.
private func roundedPolygon(_ pts: [CGPoint], _ radius: CGFloat) -> Path {
    func unit(_ a: CGPoint, _ b: CGPoint) -> CGVector {
        let dx = b.x - a.x, dy = b.y - a.y
        let len = max(sqrt(dx * dx + dy * dy), 0.0001)
        return CGVector(dx: dx / len, dy: dy / len)
    }
    var path = Path()
    let n = pts.count
    for i in 0..<n {
        let cur = pts[i]
        let prev = pts[(i - 1 + n) % n]
        let next = pts[(i + 1) % n]

        let inVec = unit(prev, cur)
        let outVec = unit(cur, next)
        let entry = CGPoint(x: cur.x - inVec.dx * radius, y: cur.y - inVec.dy * radius)
        let exit = CGPoint(x: cur.x + outVec.dx * radius, y: cur.y + outVec.dy * radius)

        if i == 0 { path.move(to: entry) } else { path.addLine(to: entry) }
        path.addQuadCurve(to: exit, control: cur)
    }
    path.closeSubpath()
    return path
}

// MARK: - Shapes

private struct GridTilesA: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = g.tile(2.4, 2.4)
        p.addPath(g.tile(13.4, 13.4))
        return p
    }
}

private struct GridTilesB: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = g.tile(13.4, 2.4)
        p.addPath(g.tile(2.4, 13.4))
        return p
    }
}

private struct MoonBody: Shape {
    func path(in rect: CGRect) -> Path {
        IconGrid(rect).ring(10.2, 10.2, 8.0)
    }
}

private struct MoonSatellite: Shape {
    func path(in rect: CGRect) -> Path {
        IconGrid(rect).ring(17.4, 17.4, 4.6)
    }
}

private struct GearHub: Shape {
    func path(in rect: CGRect) -> Path {
        IconGrid(rect).ring(12, 12, 5.6)
    }
}

private struct GearBody: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        let c = g.p(12, 12)
        let teeth = 8
        let step = .pi * 2 / CGFloat(teeth)
        let half = step * 0.30
        var p = Path()
        for i in 0..<teeth {
            let a = CGFloat(i) * step
            p.addArc(center: c, radius: g.r(11.4),
                     startAngle: .radians(a - half), endAngle: .radians(a + half),
                     clockwise: false)
            p.addArc(center: c, radius: g.r(8.6),
                     startAngle: .radians(a + half), endAngle: .radians(a + step - half),
                     clockwise: false)
        }
        p.closeSubpath()
        p.addPath(g.ring(12, 12, 5.2))
        return p
    }
}

private struct WarningGlyph: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = roundedPolygon([g.p(12, 2.4), g.p(22.4, 20.6), g.p(1.6, 20.6)], g.r(2.4))
        p.addPath(g.capsule(10.9, 8.6, 2.2, 6.4))
        p.addPath(g.ring(12, 17.6, 1.25))
        return p
    }
}

private struct TrayGlyph: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        return Path(roundedRect: g.box(2.8, 14.6, 18.4, 6.6),
                    cornerSize: CGSize(width: g.r(2.2), height: g.r(2.2)))
    }
}

private struct ArrowShaft: Shape {
    func path(in rect: CGRect) -> Path {
        IconGrid(rect).capsule(10.7, 1.6, 2.6, 9.0)
    }
}

private struct ArrowHead: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        return roundedPolygon([g.p(12, 14.8), g.p(6.6, 8.4), g.p(17.4, 8.4)], g.r(1.2))
    }
}

private struct LinkRing: Shape {
    let top: Bool
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        let c = top ? g.p(14.7, 9.3) : g.p(9.3, 14.7)
        let outer = Path(roundedRect: CGRect(x: c.x - g.r(6.6), y: c.y - g.r(3.5),
                                             width: g.r(13.2), height: g.r(7.0)),
                         cornerSize: CGSize(width: g.r(3.5), height: g.r(3.5)))
        let inner = Path(roundedRect: CGRect(x: c.x - g.r(4.1), y: c.y - g.r(1.2),
                                             width: g.r(8.2), height: g.r(2.4)),
                         cornerSize: CGSize(width: g.r(1.2), height: g.r(1.2)))
        let t = CGAffineTransform(translationX: c.x, y: c.y)
            .rotated(by: -.pi / 4)
            .translatedBy(x: -c.x, y: -c.y)
        var p = outer.applying(t)
        p.addPath(inner.applying(t))
        return p
    }
}

private struct InfoGlyph: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = g.ring(12, 12, 9.6)
        p.addPath(g.ring(12, 7.6, 1.5))
        p.addPath(g.capsule(10.8, 10.4, 2.4, 6.6))
        return p
    }
}

private struct CheckGlyph: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = Path()
        p.move(to: g.p(7.0, 12.4))
        p.addLine(to: g.p(10.4, 15.9))
        p.addLine(to: g.p(17.2, 8.4))
        return p
    }
}

private struct CloseGlyph: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = Path()
        p.move(to: g.p(8.4, 8.4)); p.addLine(to: g.p(15.6, 15.6))
        p.move(to: g.p(15.6, 8.4)); p.addLine(to: g.p(8.4, 15.6))
        return p
    }
}

private struct CameraBody: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        var p = Path(roundedRect: g.box(1.8, 6.6, 20.4, 13.8),
                     cornerSize: CGSize(width: g.r(2.8), height: g.r(2.8)))
        p.addPath(g.ring(12, 13.5, 4.3))
        p.addPath(g.ring(19.4, 9.6, 1.1))
        return p
    }
}

private struct CameraLens: Shape {
    func path(in rect: CGRect) -> Path {
        IconGrid(rect).ring(12, 13.5, 4.3)
    }
}

private struct CameraBump: Shape {
    func path(in rect: CGRect) -> Path {
        let g = IconGrid(rect)
        return Path(roundedRect: g.box(4.0, 3.6, 6.6, 3.6),
                    cornerSize: CGSize(width: g.r(1.4), height: g.r(1.4)))
    }
}
