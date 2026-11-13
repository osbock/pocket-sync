// Draws the Pocket Recordings app icon (1024x1024 PNG).
// Usage: swift make_icon.swift OUT.png
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon_1024.png"

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// macOS icon grid: 824pt rounded square centred in 1024.
let inset: CGFloat = 100
let body = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let shape = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
NSGradient(starting: NSColor(calibratedRed: 0.20, green: 0.45, blue: 0.93, alpha: 1),
           ending: NSColor(calibratedRed: 0.12, green: 0.20, blue: 0.55, alpha: 1))!
    .draw(in: shape, angle: -90)

// Waveform bars.
let heights: [CGFloat] = [0.18, 0.34, 0.55, 0.80, 0.62, 0.95, 0.70, 0.45, 0.66, 0.38, 0.22]
let barW: CGFloat = 38, gap: CGFloat = 22
let total = CGFloat(heights.count) * barW + CGFloat(heights.count - 1) * gap
var x = (size - total) / 2
let midY = size / 2 + 20
NSColor.white.setFill()
for h in heights {
    let bh = h * 430
    NSBezierPath(roundedRect: NSRect(x: x, y: midY - bh / 2, width: barW, height: bh),
                 xRadius: barW / 2, yRadius: barW / 2).fill()
    x += barW + gap
}

// A small "pocket" lip along the bottom.
NSColor(calibratedWhite: 1, alpha: 0.22).setFill()
NSBezierPath(roundedRect: NSRect(x: 250, y: 215, width: 524, height: 64), xRadius: 32, yRadius: 32).fill()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
