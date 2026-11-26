package com.jarvis.companion

import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import com.google.zxing.client.android.R as ZxingR
import com.journeyapps.barcodescanner.CameraPreview
import com.journeyapps.barcodescanner.CaptureActivity
import com.journeyapps.barcodescanner.DecoratedBarcodeView

/** Compatibility scanner retained as a manually selectable fallback and benchmark comparator. */
class PairingZxingCaptureActivity : CaptureActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val attemptId = intent.getStringExtra(EXTRA_ATTEMPT_ID).orEmpty()
        val startedMs = intent.getLongExtra(EXTRA_STARTED_MS, SystemClock.elapsedRealtime())
        val scanner = findViewById<DecoratedBarcodeView>(ZxingR.id.zxing_barcode_scanner)
        val settings = scanner.cameraSettings.apply {
            setAutoFocusEnabled(true)
            setContinuousFocusEnabled(true)
            setBarcodeSceneModeEnabled(true)
            setExposureEnabled(true)
            setMeteringEnabled(true)
        }
        scanner.cameraSettings = settings
        scanner.barcodeView.addStateListener(object : CameraPreview.StateListener {
            override fun previewSized() = Unit
            override fun previewStarted() {
                Log.i(TAG, "preview_ready engine=ZXING attempt=$attemptId since_start_ms=${SystemClock.elapsedRealtime() - startedMs}")
            }
            override fun previewStopped() = Unit
            override fun cameraError(exception: Exception) {
                Log.w(TAG, "camera_error engine=ZXING attempt=$attemptId type=${exception.javaClass.simpleName}")
            }
            override fun cameraClosed() = Unit
        })
        Log.i(TAG, "camera_configured engine=ZXING attempt=$attemptId focus=continuous barcode_scene=true")
    }

    companion object {
        const val EXTRA_ATTEMPT_ID = "com.jarvis.companion.extra.QR_ATTEMPT_ID"
        const val EXTRA_STARTED_MS = "com.jarvis.companion.extra.QR_STARTED_MS"
        private const val TAG = "ThozhaPairingQr"
    }
}
