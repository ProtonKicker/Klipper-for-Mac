// Renders AppIcon.iconset PNGs (Apple-style squircle, gradient, nozzle + filament).
// Run: xcrun swift Resources/make-icon.swift  (from the KlipperMac/ dir)
import AppKit

let sizes: [(name: String, px: Int)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]

let outDir = URL(fileURLWithPath: "AppIcon.iconset", relativeTo: URL(fileURLWithPath: FileManager.default.currentDirectoryPath))
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

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

    // squircle tile
    let margin: CGFloat = 64
    let rect = CGRect(x: margin, y: margin, width: 1024 - 2 * margin, height: 1024 - 2 * margin)
    let radius: CGFloat = rect.width * 0.2237
    let path = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
    ctx.addPath(path)
    ctx.clip()

    let colors = [NSColor(calibratedRed: 0.22, green: 0.47, blue: 0.93, alpha: 1).cgColor,
                  NSColor(calibratedRed: 0.07, green: 0.20, blue: 0.55, alpha: 1).cgColor]
    if let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                             colors: colors as CFArray, locations: [0, 1]) {
        ctx.drawLinearGradient(grad, start: CGPoint(x: 512, y: 960),
                               end: CGPoint(x: 512, y: 64), options: [])
    }
    // soft top glow
    if let glow = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                             colors: [NSColor(white: 1, alpha: 0.28).cgColor,
                                      NSColor(white: 1, alpha: 0).cgColor] as CFArray,
                             locations: [0, 1]) {
        ctx.drawRadialGradient(glow, startCenter: CGPoint(x: 320, y: 820), startRadius: 0,
                               endCenter: CGPoint(x: 320, y: 820), endRadius: 620, options: [])
    }
    ctx.resetClip()

    // nozzle body (silver, rounded trapezoid) + heat-sink fins, centered upper
    let silver = NSColor(calibratedWhite: 0.96, alpha: 1).cgColor
    func roundRect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ r: CGFloat) {
        ctx.addPath(CGPath(roundedRect: CGRect(x: x, y: y, width: w, height: h),
                           cornerWidth: r, cornerHeight: r, transform: nil))
        ctx.fillPath()
    }
    ctx.setFillColor(silver)
    // fins
    for i in 0..<4 {
        let y = 700 - CGFloat(i) * 52
        roundRect(300, y, 424, 30, 12)
    }
    // body: taper via two stacked rounded rects
    roundRect(380, 500, 264, 180, 40)
    roundRect(430, 420, 164, 120, 28)
    // tip triangle
    ctx.beginPath()
    ctx.move(to: CGPoint(x: 460, y: 430))
    ctx.addLine(to: CGPoint(x: 564, y: 430))
    ctx.addLine(to: CGPoint(x: 512, y: 330))
    ctx.closePath()
    ctx.fillPath()
    // heater band (orange) on the tip neck
    ctx.setFillColor(NSColor(calibratedRed: 0.98, green: 0.55, blue: 0.15, alpha: 1).cgColor)
    roundRect(436, 452, 152, 44, 12)
    // filament ribbon: smooth curve from the tip downward
    ctx.setStrokeColor(NSColor(calibratedRed: 0.93, green: 0.97, blue: 1.0, alpha: 1).cgColor)
    ctx.setLineWidth(34)
    ctx.setLineCap(.round)
    ctx.beginPath()
    ctx.move(to: CGPoint(x: 512, y: 330))
    ctx.addCurve(to: CGPoint(x: 640, y: 150),
                 control1: CGPoint(x: 430, y: 230), control2: CGPoint(x: 700, y: 260))
    ctx.strokePath()
    // droplet end
    ctx.setFillColor(NSColor(calibratedRed: 0.93, green: 0.97, blue: 1.0, alpha: 1).cgColor)
    ctx.fillEllipse(in: CGRect(x: 618, y: 128, width: 46, height: 46))

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

for s in sizes {
    if let data = draw(s.px) {
        try? data.write(to: outDir.appendingPathComponent(s.name))
    }
}
print("wrote \(sizes.count) pngs to AppIcon.iconset/")
