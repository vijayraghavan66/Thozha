package com.jarvis.companion

import android.os.Bundle
import android.util.Log
import com.google.zxing.client.android.R as ZxingR
import com.journeyapps.barcodescanner.CaptureActivity
import com.journeyapps.barcodescanner.DecoratedBarcodeView

/** ZXing capture activity tuned for dense pairing QRs on mobile camera hardware. */
class PairingQrCaptureActivity : CaptureActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val scanner = findViewById<DecoratedBarcodeView>(ZxingR.id.zxing_barcode_scanner)

        val settings = scanner.cameraSettings
        settings.setAutoFocusEnabled(true)
        settings.setContinuousFocusEnabled(true)
        settings.setBarcodeSceneModeEnabled(true)
        settings.setExposureEnabled(true)
        settings.setMeteringEnabled(true)
        scanner.cameraSettings = settings
        Log.i(TAG, "scanner_configured focus=continuous barcode_scene=true exposure=true metering=true")
    }

    companion object {
        private const val TAG = "ThozhaPairingQr"
    }
}
