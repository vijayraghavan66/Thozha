package com.jarvis.companion.data

import android.graphics.Bitmap
import android.os.SystemClock
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.common.InputImage
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant
import java.util.EnumMap
import java.util.concurrent.TimeUnit

/** On-device decoder-model benchmark using deterministic fixtures; this is not an optical-camera test. */
@RunWith(AndroidJUnit4::class)
class PairingQrDecoderInstrumentedTest {
    private val testNow = Instant.parse("2030-01-02T03:00:00Z")

    @Test
    fun mlKitAndZxingDecodeCompactAndFullSignedInvitations() {
        val context = InstrumentationRegistry.getInstrumentation().context
        val full = context.assets.open("pairing_v2_full_cross_language.json")
            .bufferedReader(Charsets.UTF_8).use { it.readText().trim() }
        val compact = context.assets.open("pairing_v2_compact_cross_language.txt")
            .bufferedReader(Charsets.UTF_8).use { it.readText().trim() }
        assertTrue("compact QR fixture should materially reduce encoded characters", compact.length < full.length * 0.65)

        val fullParsed = PairingInvitationParser.parse(full, now = testNow)
        val compactParsed = PairingInvitationParser.parse(compact, now = testNow)
        assertEquals(fullParsed.invitationId, compactParsed.invitationId)
        assertEquals(fullParsed.serverFingerprint, compactParsed.serverFingerprint)
        assertEquals(fullParsed.gatewayCertificateSha256, compactParsed.gatewayCertificateSha256)

        val images = mapOf("full" to qrBitmap(full), "compact" to qrBitmap(compact))
        val scanner = BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build(),
        )
        try {
            for ((label, bitmap) in images) {
                val coldStart = SystemClock.elapsedRealtime()
                val first = decodeMlKit(scanner, bitmap)
                val coldMs = SystemClock.elapsedRealtime() - coldStart
                assertEquals(if (label == "full") full else compact, first)

                val warmSamples = ArrayList<Long>(10)
                repeat(10) {
                    val start = SystemClock.elapsedRealtime()
                    val decoded = decodeMlKit(scanner, bitmap)
                    val elapsed = SystemClock.elapsedRealtime() - start
                    assertEquals(if (label == "full") full else compact, decoded)
                    warmSamples += elapsed
                }
                Log.i(TAG, "synthetic_decode engine=MLKIT payload=$label chars=${if (label == "full") full.length else compact.length} cold_ms=$coldMs warm_ms=${warmSamples.joinToString(",")}")

                val zxingSamples = ArrayList<Long>(10)
                repeat(10) {
                    val start = SystemClock.elapsedRealtime()
                    val decoded = decodeZxing(bitmap)
                    val elapsed = SystemClock.elapsedRealtime() - start
                    assertEquals(if (label == "full") full else compact, decoded)
                    zxingSamples += elapsed
                }
                Log.i(TAG, "synthetic_decode engine=ZXING payload=$label chars=${if (label == "full") full.length else compact.length} warm_ms=${zxingSamples.joinToString(",")}")
            }
        } finally {
            scanner.close()
            images.values.forEach(Bitmap::recycle)
        }
    }

    private fun decodeMlKit(scanner: com.google.mlkit.vision.barcode.BarcodeScanner, bitmap: Bitmap): String {
        val barcodes = Tasks.await(scanner.process(InputImage.fromBitmap(bitmap, 0)), 10, TimeUnit.SECONDS)
        return barcodes.firstOrNull { it.format == Barcode.FORMAT_QR_CODE }?.rawValue
            ?: throw AssertionError("Bundled ML Kit returned no QR payload")
    }

    private fun decodeZxing(bitmap: Bitmap): String {
        val pixels = IntArray(bitmap.width * bitmap.height)
        bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
        val hints = EnumMap<DecodeHintType, Any>(DecodeHintType::class.java).apply {
            put(DecodeHintType.TRY_HARDER, true)
        }
        return MultiFormatReader().decode(
            BinaryBitmap(HybridBinarizer(RGBLuminanceSource(bitmap.width, bitmap.height, pixels))),
            hints,
        ).text
    }

    private fun qrBitmap(value: String): Bitmap {
        val matrix = QRCodeWriter().encode(
            value,
            BarcodeFormat.QR_CODE,
            1280,
            1280,
            mapOf(
                com.google.zxing.EncodeHintType.CHARACTER_SET to "UTF-8",
                com.google.zxing.EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.L,
            ),
        )
        val pixels = IntArray(matrix.width * matrix.height) { index ->
            if (matrix.get(index % matrix.width, index / matrix.width)) 0xff000000.toInt() else -1
        }
        return Bitmap.createBitmap(matrix.width, matrix.height, Bitmap.Config.ARGB_8888).apply {
            setPixels(pixels, 0, matrix.width, 0, 0, matrix.width, matrix.height)
        }
    }

    companion object {
        private const val TAG = "ThozhaPairingQrTest"
    }
}
