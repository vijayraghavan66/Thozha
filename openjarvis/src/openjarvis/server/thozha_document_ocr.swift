import AppKit
import CoreGraphics
import Foundation
import ImageIO
import PDFKit
import Vision

enum DocumentOcrError: Error {
    case invalidArguments
    case invalidImage
    case invalidPdf
    case pageLimit
    case imageLimit
    case noText
    case outputLimit

    var code: String {
        switch self {
        case .invalidArguments: return "invalidArguments"
        case .invalidImage: return "invalidImage"
        case .invalidPdf: return "invalidPdf"
        case .pageLimit: return "pageLimit"
        case .imageLimit: return "imageLimit"
        case .noText: return "noText"
        case .outputLimit: return "outputLimit"
        }
    }
}

func recognize(_ image: CGImage) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        .joined(separator: "\n")
}

func renderAndRecognize(_ page: PDFPage) throws -> String {
    let bounds = page.bounds(for: .mediaBox).integral
    guard bounds.width > 0, bounds.height > 0 else { throw DocumentOcrError.invalidPdf }
    let scale = min(2.0, 5000.0 / max(bounds.width, bounds.height))
    let width = max(1, Int(bounds.width * scale))
    let height = max(1, Int(bounds.height * scale))
    guard width * height <= 25_000_000 else { throw DocumentOcrError.imageLimit }
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(
              data: nil,
              width: width,
              height: height,
              bitsPerComponent: 8,
              bytesPerRow: 0,
              space: colorSpace,
              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
          ) else { throw DocumentOcrError.invalidPdf }
    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()
    guard let image = context.makeImage() else { throw DocumentOcrError.invalidPdf }
    return try recognize(image)
}

func extract(_ url: URL, mediaType: String) throws -> (text: String, pages: Int) {
    var text = ""
    var pages = 1
    if mediaType == "application/pdf" {
        guard let document = PDFDocument(url: url), document.pageCount > 0 else {
            throw DocumentOcrError.invalidPdf
        }
        guard document.pageCount <= 10 else { throw DocumentOcrError.pageLimit }
        pages = document.pageCount
        var pageText: [String] = []
        for index in 0..<document.pageCount {
            guard let page = document.page(at: index) else { throw DocumentOcrError.invalidPdf }
            let embedded = page.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let recognized = embedded.isEmpty ? try renderAndRecognize(page) : embedded
            if !recognized.isEmpty { pageText.append(recognized) }
        }
        text = pageText.joined(separator: "\n\n")
    } else if mediaType == "image/png" || mediaType == "image/jpeg" {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw DocumentOcrError.invalidImage
        }
        guard image.width <= 10000, image.height <= 10000,
              image.width * image.height <= 25_000_000 else {
            throw DocumentOcrError.imageLimit
        }
        text = try recognize(image)
    } else {
        throw DocumentOcrError.invalidArguments
    }
    let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !cleaned.isEmpty else { throw DocumentOcrError.noText }
    guard cleaned.utf8.count <= 100_000 else { throw DocumentOcrError.outputLimit }
    return (cleaned, pages)
}

func main() {
    guard CommandLine.arguments.count == 3 else {
        fputs("invalid arguments\n", stderr)
        exit(2)
    }
    let url = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: false)
    let mediaType = CommandLine.arguments[2]
    do {
        let result = try extract(url, mediaType: mediaType)
        let data = try JSONSerialization.data(withJSONObject: [
            "text": result.text,
            "page_count": result.pages,
        ], options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0a]))
    } catch let error as DocumentOcrError {
        fputs("document extraction failed: \(error.code)\n", stderr)
        exit(1)
    } catch {
        fputs("document extraction failed: runtimeError\n", stderr)
        exit(1)
    }
}

main()
