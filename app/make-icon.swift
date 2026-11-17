// Generates Resources/AppIcon.icns: a slanted envelope with speed lines on a dark tile.
// Run from app/: swift make-icon.swift
import AppKit

func render(_ px: Int) -> Data {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext
    let u = s / 1024 // design on a 1024 grid

    // Tile (macOS icon grid: 824 x 824 centered, radius ~185)
    let tile = CGRect(x: 100 * u, y: 100 * u, width: 824 * u, height: 824 * u)
    let tilePath = CGPath(roundedRect: tile, cornerWidth: 185 * u, cornerHeight: 185 * u, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10 * u), blur: 28 * u, color: NSColor.black.withAlphaComponent(0.35).cgColor)
    ctx.addPath(tilePath); ctx.setFillColor(NSColor.black.cgColor); ctx.fillPath()
    ctx.restoreGState()
    ctx.saveGState()
    ctx.addPath(tilePath); ctx.clip()
    let bg = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [
        NSColor(red: 0.13, green: 0.13, blue: 0.16, alpha: 1).cgColor,
        NSColor(red: 0.04, green: 0.04, blue: 0.06, alpha: 1).cgColor] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(bg, start: CGPoint(x: 0, y: tile.maxY), end: CGPoint(x: 0, y: tile.minY), options: [])
    ctx.restoreGState()

    let accent = NSColor(red: 0.20, green: 0.92, blue: 0.62, alpha: 1).cgColor
    ctx.setLineCap(.round)
    ctx.setLineJoin(.round)

    // Envelope, slanted forward
    ctx.saveGState()
    ctx.translateBy(x: 560 * u, y: 512 * u)
    ctx.concatenate(CGAffineTransform(a: 1, b: 0, c: 0.22, d: 1, tx: 0, ty: 0))
    let w: CGFloat = 400 * u, h: CGFloat = 290 * u
    let env = CGRect(x: -w / 2, y: -h / 2, width: w, height: h)
    ctx.setStrokeColor(NSColor.white.cgColor)
    ctx.setLineWidth(44 * u)
    ctx.addPath(CGPath(roundedRect: env, cornerWidth: 46 * u, cornerHeight: 46 * u, transform: nil))
    ctx.strokePath()
    ctx.move(to: CGPoint(x: env.minX + 40 * u, y: env.maxY - 34 * u))
    ctx.addLine(to: CGPoint(x: 0, y: env.midY + 8 * u))
    ctx.addLine(to: CGPoint(x: env.maxX - 40 * u, y: env.maxY - 34 * u))
    ctx.strokePath()
    ctx.restoreGState()

    // Speed lines
    ctx.setStrokeColor(accent)
    ctx.setLineWidth(34 * u)
    for (y, x0, x1) in [(612.0, 214.0, 318.0), (512.0, 170.0, 300.0), (412.0, 236.0, 306.0)] {
        ctx.move(to: CGPoint(x: x0 * u, y: y * u))
        ctx.addLine(to: CGPoint(x: x1 * u, y: y * u))
    }
    ctx.strokePath()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let dir = URL(fileURLWithPath: "Resources/AppIcon.iconset")
try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    try! render(base).write(to: dir.appendingPathComponent("icon_\(base)x\(base).png"))
    try! render(base * 2).write(to: dir.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
try! render(1024).write(to: URL(fileURLWithPath: "Resources/icon-preview.png"))
