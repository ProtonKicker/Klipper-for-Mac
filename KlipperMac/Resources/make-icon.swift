// Renders AppIcon.iconset PNGs — minimal Apple-style icon:
// white squircle, bottom-up view of a nozzle (concentric rings) with a soft
// orange heat glow at the center.
// Run from KlipperMac/:  xcrun swift Resources/make-icon.swift
// Then:  iconutil -c icns AppIcon.iconset -o Resources/AppIcon.icns
import AppKit
import CoreGraphics
import Foundation

let sizes: [(name: String, px: Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]

let outDir = URL(fileURLWithPath: "AppIcon.iconset", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath))
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

// Continuous-corner squircle (superellipse) centered at (cx, cy).
func squirclePath(cx: CGFloat, cy: CGFloat, a: CGFloat, ratio: CGFloat = 4.6) -> CGPath {
    let path = CGMutablePath()
    let n = 2.0 / ratio
    let steps = 512
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let x = cx + a * (cos(t) >= 0 ? 1 : -1) * pow(abs(cos(t)), n)
        let y = cy + a * (sin(t) >= 0 ? 1 : -1) * pow(abs(sin(t)), n)
        if i == 0 { path.move(to: CGPoint(x: x, y: y)) } else { path.addLine(to: CGPoint(x: x, y: y)) }
    }
    path.closeSubpath()
    return path
}

func draw(_ S: Int) -> Data? {
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: S, pixelsHigh: S,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                     isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
    rep.size = NSSize(width: S, height: S)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    guard let ctx = NSGraphicsContext.current?.cgContext else { return nil }
    ctx.scaleBy(x: CGFloat(S) / 1024.0, y: CGFloat(S) / 1024.0)
    ctx.setShouldAntialias(true)
    let cs = CGColorSpaceCreateDeviceRGB()
    let C = CGPoint(x: 512, y: 512)

    // ---- white squircle tile ----
    let tile = squirclePath(cx: 512, cy: 512, a: 420)
    ctx.saveGState()
    ctx.addPath(tile)
    ctx.clip()
    if let g = CGGradient(colorsSpace: cs, colors: [
        NSColor(calibratedWhite: 1.0, alpha: 1).cgColor,
        NSColor(calibratedRed: 0.945, green: 0.955, blue: 0.97, alpha: 1).cgColor] as CFArray,
        locations: [0, 1]) {
        ctx.drawLinearGradient(g, start: CGPoint(x: 512, y: 932), end: CGPoint(x: 512, y: 92), options: [])
    }
    ctx.restoreGState()
    // hairline border so the white tile reads on light desktops
    ctx.saveGState()
    ctx.addPath(tile)
    ctx.clip()
    ctx.addPath(tile)
    ctx.setLineWidth(4)
    ctx.setStrokeColor(NSColor(calibratedRed: 0.72, green: 0.76, blue: 0.82, alpha: 0.9).cgColor)
    ctx.strokePath()
    ctx.restoreGState()

    // ---- everything below is clipped to the tile ----
    ctx.saveGState()
    ctx.addPath(tile)
    ctx.clip()

    func disk(_ r: CGFloat, _ colors: [NSColor], locations: [CGFloat], startR: CGFloat) {
        ctx.saveGState()
        ctx.addEllipse(in: CGRect(x: C.x - r, y: C.y - r, width: 2 * r, height: 2 * r))
        ctx.clip()
        if let g = CGGradient(colorsSpace: cs, colors: colors.map(\.cgColor) as CFArray,
                              locations: locations) {
            ctx.drawRadialGradient(g, startCenter: C, startRadius: startR,
                                   endCenter: C, endRadius: r, options: [])
        }
        ctx.restoreGState()
    }
    func ring(_ r: CGFloat, width: CGFloat, _ color: NSColor) {
        ctx.addEllipse(in: CGRect(x: C.x - r, y: C.y - r, width: 2 * r, height: 2 * r))
        ctx.setStrokeColor(color.cgColor)
        ctx.setLineWidth(width)
        ctx.strokePath()
    }

    // soft shadow under the whole circular assembly
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 60,
                  color: NSColor(calibratedRed: 0.35, green: 0.42, blue: 0.55, alpha: 0.35).cgColor)
    ctx.addEllipse(in: CGRect(x: C.x - 330, y: C.y - 330, width: 660, height: 660))
    ctx.setFillColor(NSColor(calibratedWhite: 0.97, alpha: 1).cgColor)
    ctx.fillPath()
    ctx.restoreGState()

    // ---- fin stack seen from below: concentric light-gray rings ----
    disk(330, [NSColor(calibratedWhite: 0.99, alpha: 1),
               NSColor(calibratedRed: 0.90, green: 0.92, blue: 0.95, alpha: 1)],
         locations: [0, 1], startR: 40)
    // fin grooves
    for i in 0..<4 {
        let r = CGFloat(322) - CGFloat(i) * 26
        ring(r, width: 10, NSColor(calibratedRed: 0.80, green: 0.83, blue: 0.88, alpha: 0.9))
        ring(r - 9, width: 3, NSColor(calibratedWhite: 1, alpha: 0.85))
    }
    // outer edge of the fin stack
    ring(330, width: 8, NSColor(calibratedRed: 0.70, green: 0.74, blue: 0.80, alpha: 1))

    // ---- heater ring: subtle machined seam ----
    ring(250, width: 10, NSColor(calibratedRed: 0.74, green: 0.77, blue: 0.83, alpha: 1))
    ring(243, width: 3, NSColor(calibratedWhite: 1, alpha: 0.9))

    // ---- cone receding into the distance: dark rings toward center ----
    disk(190, [NSColor(calibratedRed: 0.93, green: 0.94, blue: 0.96, alpha: 1),
               NSColor(calibratedRed: 0.62, green: 0.66, blue: 0.73, alpha: 1)],
         locations: [0, 1], startR: 100)
    disk(112, [NSColor(calibratedRed: 0.24, green: 0.26, blue: 0.31, alpha: 1),
               NSColor(calibratedRed: 0.09, green: 0.10, blue: 0.14, alpha: 1)],
         locations: [0, 1], startR: 40)

    // ---- the orange blur: molten glow spilling out of the orifice ----
    disk(165, [NSColor(calibratedRed: 1.0, green: 0.60, blue: 0.14, alpha: 0.85),
               NSColor(calibratedRed: 1.0, green: 0.48, blue: 0.09, alpha: 0.35),
               NSColor(calibratedRed: 1.0, green: 0.45, blue: 0.08, alpha: 0)],
         locations: [0, 0.45, 1], startR: 12)

    // hot orifice core
    disk(48, [NSColor(calibratedRed: 1.0, green: 0.97, blue: 0.88, alpha: 1),
              NSColor(calibratedRed: 1.0, green: 0.78, blue: 0.30, alpha: 1),
              NSColor(calibratedRed: 1.0, green: 0.52, blue: 0.10, alpha: 1),
              NSColor(calibratedRed: 0.85, green: 0.35, blue: 0.04, alpha: 1)],
         locations: [0, 0.35, 0.75, 1], startR: 2)
    // tiny dark hole at the very center
    disk(13, [NSColor(calibratedRed: 0.45, green: 0.12, blue: 0.01, alpha: 1),
              NSColor(calibratedRed: 0.20, green: 0.05, blue: 0.01, alpha: 1)],
         locations: [0, 1], startR: 1)

    ctx.restoreGState() // end tile clip
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

for s in sizes {
    if let data = draw(s.px) {
        try? data.write(to: outDir.appendingPathComponent(s.name))
    } else {
        FileHandle.standardError.write(Data("failed: \(s.name)\n".utf8))
    }
}
print("wrote \(sizes.count) pngs to AppIcon.iconset/")
