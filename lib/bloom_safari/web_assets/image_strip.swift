// Local crop/stitch/resize only. Input pixels come from the owned Safari window.
import Foundation
import CoreGraphics
import ImageIO

struct Part: Decodable { let path: String; let x: Int; let y: Int; let width: Int; let height: Int; let outputHeight: Int }
struct Job: Decodable { let output: String; let width: Int; let parts: [Part] }
do {
    let job = try JSONDecoder().decode(Job.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
    let height = job.parts.reduce(0) { $0 + $1.outputHeight }
    guard job.width > 0, job.width <= 2048, height > 0, height <= 8192,
          let context = CGContext(data: nil, width: job.width, height: height, bitsPerComponent: 8, bytesPerRow: job.width*4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { throw NSError(domain:"image_strip",code:1) }
    context.setFillColor(CGColor(gray:1,alpha:1)); context.fill(CGRect(x:0,y:0,width:job.width,height:height))
    context.interpolationQuality = .high
    var offset = 0
    for part in job.parts {
        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath:part.path) as CFURL,nil),
              let image = CGImageSourceCreateImageAtIndex(source,0,nil) else { throw NSError(domain:"image_strip_decode",code:2,userInfo:[NSLocalizedDescriptionKey:part.path]) }
        guard
              part.x >= 0, part.y >= 0, part.width > 0, part.height > 0,
              part.x+part.width <= image.width, part.y+part.height <= image.height,
              let crop = image.cropping(to:CGRect(x:part.x,y:part.y,width:part.width,height:part.height)) else { throw NSError(domain:"image_strip_bounds",code:2,userInfo:[NSLocalizedDescriptionKey:"source \(image.width)x\(image.height), crop \(part.x),\(part.y),\(part.width),\(part.height)"]) }
        context.draw(crop,in:CGRect(x:0,y:height-offset-part.outputHeight,width:job.width,height:part.outputHeight))
        offset += part.outputHeight
    }
    guard let result = context.makeImage(), let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath:job.output) as CFURL,"public.png" as CFString,1,nil) else { throw NSError(domain:"image_strip",code:3) }
    CGImageDestinationAddImage(destination,result,nil)
    guard CGImageDestinationFinalize(destination) else { throw NSError(domain:"image_strip",code:4) }
    print("{\"width\":\(job.width),\"height\":\(height)}")
} catch {
    FileHandle.standardError.write(Data("Image strip failed: \(error)\n".utf8)); exit(1)
}
