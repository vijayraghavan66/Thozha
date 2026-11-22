import CoreImage
import CoreImage.CIFilterBuiltins
import CoreGraphics
import Foundation

public enum PairingQRCodeGenerator {
    public static func image(payload: String) -> CGImage? {
        guard !payload.isEmpty, payload.utf8.count <= 8192 else { return nil }
        let generator = CIFilter.qrCodeGenerator()
        generator.message = Data(payload.utf8)
        generator.correctionLevel = "Q"
        guard let output = generator.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)) else { return nil }
        return CIContext().createCGImage(output, from: output.extent)
    }
}
