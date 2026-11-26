package com.jarvis.companion

import android.content.Intent
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Canvas
import android.graphics.RectF
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.camera.view.PreviewView.StreamState
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** CameraX + bundled, on-device ML Kit QR path. No image or decoded text is persisted. */
class PairingQrCaptureActivity : ComponentActivity() {
    private val attemptId = java.util.UUID.randomUUID().toString().take(8)
    private val attemptStartedMs = SystemClock.elapsedRealtime()
    private val finished = AtomicBoolean(false)
    private val analyzerExecutor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var previewView: PreviewView
    private lateinit var status: TextView
    private lateinit var scanner: BarcodeScanner
    private var provider: ProcessCameraProvider? = null
    private var previewUseCase: Preview? = null
    private var analysisUseCase: ImageAnalysis? = null
    private var camera: Camera? = null
    private var previewReadyMs: Long? = null
    private var firstFrameLogged = false
    private var failureLogged = false
    private val slowScanRunnable = Runnable {
        if (!finished.get()) {
            Log.w(TAG, "scan_timeout engine=MLKIT attempt=$attemptId threshold_ms=5000 total_ms=${SystemClock.elapsedRealtime() - attemptStartedMs}")
            status.text = "Still looking · move back until the code is sharp, or choose ZXing fallback"
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Color.BLACK
        window.navigationBarColor = Color.BLACK
        scanner = BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build(),
        )
        buildScannerUi()
        Log.i(TAG, "scan_started engine=MLKIT attempt=$attemptId mono_ms=$attemptStartedMs")
        mainHandler.postDelayed(slowScanRunnable, 5_000)
        bindCamera()
    }

    private fun buildScannerUi() {
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        previewView = PreviewView(this).apply {
            implementationMode = PreviewView.ImplementationMode.PERFORMANCE
            scaleType = PreviewView.ScaleType.FILL_CENTER
        }
        root.addView(previewView, FrameLayout.LayoutParams(-1, -1))
        root.addView(ScannerGuideView())

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), dp(28), dp(24), dp(18))
            setBackgroundColor(0x99090B09.toInt())
        }
        header.addView(TextView(this).apply {
            text = "Scan Thozha invitation"
            textSize = 21f
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
        })
        status = TextView(this).apply {
            text = "Hold the Mac QR in the frame. Scanning stays on this phone."
            textSize = 14f
            setTextColor(0xffd2d8d1.toInt())
            gravity = Gravity.CENTER
            setPadding(0, dp(8), 0, 0)
        }
        header.addView(status)
        root.addView(header, FrameLayout.LayoutParams(-1, -2, Gravity.TOP))

        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(dp(16), dp(12), dp(16), dp(18))
            setBackgroundColor(0xaa090B09.toInt())
        }
        val torch = Button(this).apply { text = "Torch" }
        torch.setOnClickListener {
            val enabled = !torch.isSelected
            camera?.cameraControl?.enableTorch(enabled)
            torch.isSelected = enabled
            torch.text = if (enabled) "Torch on" else "Torch"
        }
        controls.addView(torch, LinearLayout.LayoutParams(0, dp(52), 1f))
        val zxing = Button(this).apply { text = "ZXing fallback" }
        zxing.setOnClickListener {
            if (finished.compareAndSet(false, true)) {
                Log.i(TAG, "fallback_selected from=MLKIT to=ZXING attempt=$attemptId after_ms=${SystemClock.elapsedRealtime() - attemptStartedMs}")
                setResult(RESULT_CANCELED, Intent().putExtra(EXTRA_USE_ZXING, true))
                finish()
            }
        }
        controls.addView(zxing, LinearLayout.LayoutParams(0, dp(52), 1f))
        val cancel = Button(this).apply { text = "Cancel" }
        cancel.setOnClickListener {
            setResult(RESULT_CANCELED)
            finish()
        }
        controls.addView(cancel, LinearLayout.LayoutParams(0, dp(52), 1f))
        root.addView(controls, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))

        previewView.setOnTouchListener { _, event ->
            if (event.action == MotionEvent.ACTION_UP) focusAt(event.x, event.y)
            true
        }
        setContentView(root)
    }

    private fun bindCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(this)
        cameraProviderFuture.addListener({
            if (isFinishing || isDestroyed) return@addListener
            try {
                provider = cameraProviderFuture.get()
                previewView.previewStreamState.observe(this) { state ->
                    if (state == StreamState.STREAMING && previewReadyMs == null) {
                        previewReadyMs = SystemClock.elapsedRealtime()
                        Log.i(TAG, "preview_ready engine=MLKIT attempt=$attemptId since_start_ms=${previewReadyMs!! - attemptStartedMs}")
                        status.text = "Camera ready · keep the signed QR in view"
                        focusAt(previewView.width / 2f, previewView.height / 2f)
                    }
                }

                val resolution = ResolutionSelector.Builder()
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            android.util.Size(1280, 720),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                        ),
                    )
                    .build()
                val preview = Preview.Builder().setResolutionSelector(resolution).build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                val analysis = ImageAnalysis.Builder()
                    .setResolutionSelector(resolution)
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                analysis.setAnalyzer(analyzerExecutor) { proxy ->
                    val mediaImage = proxy.image
                    if (mediaImage == null || finished.get()) {
                        proxy.close()
                        return@setAnalyzer
                    }
                    if (!firstFrameLogged) {
                        firstFrameLogged = true
                        Log.i(
                            TAG,
                            "first_frame engine=MLKIT attempt=$attemptId since_start_ms=${SystemClock.elapsedRealtime() - attemptStartedMs} size=${proxy.width}x${proxy.height} rotation=${proxy.imageInfo.rotationDegrees}",
                        )
                    }
                    val input = InputImage.fromMediaImage(mediaImage, proxy.imageInfo.rotationDegrees)
                    scanner.process(input)
                        .addOnSuccessListener(analyzerExecutor) { barcodes ->
                            val decoded = barcodes.asSequence()
                                .filter { it.format == Barcode.FORMAT_QR_CODE }
                                .mapNotNull { it.rawValue }
                                .firstOrNull { it.isNotBlank() && it.length <= MAX_QR_CHARS }
                            if (decoded != null && finished.compareAndSet(false, true)) {
                                val now = SystemClock.elapsedRealtime()
                                val ready = previewReadyMs ?: attemptStartedMs
                                Log.i(
                                    TAG,
                                    "decode_success engine=MLKIT attempt=$attemptId from_preview_ms=${now - ready} total_ms=${now - attemptStartedMs} chars=${decoded.length}",
                                )
                                runOnUiThread {
                                    setResult(RESULT_OK, Intent().putExtra(EXTRA_QR_PAYLOAD, decoded))
                                    finish()
                                }
                            }
                        }
                        .addOnFailureListener(analyzerExecutor) { error ->
                            if (!failureLogged) {
                                failureLogged = true
                                Log.w(TAG, "decode_error engine=MLKIT attempt=$attemptId type=${error.javaClass.simpleName}")
                                runOnUiThread { status.text = "Scanner error · use ZXing fallback or paste the invitation" }
                            }
                        }
                        .addOnCompleteListener(analyzerExecutor) { proxy.close() }
                }
                provider?.unbindAll()
                camera = provider?.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
                previewUseCase = preview
                analysisUseCase = analysis
                Log.i(TAG, "camera_bound engine=MLKIT attempt=$attemptId requested=1280x720 backpressure=keep_latest")
            } catch (error: Exception) {
                Log.e(TAG, "camera_start_error engine=MLKIT attempt=$attemptId type=${error.javaClass.simpleName}")
                status.text = "Camera unavailable · use ZXing fallback or paste the invitation"
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun focusAt(x: Float, y: Float) {
        val activeCamera = camera ?: return
        if (previewView.width <= 0 || previewView.height <= 0) return
        val point = previewView.meteringPointFactory.createPoint(x, y)
        val action = FocusMeteringAction.Builder(
            point,
            FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE,
        ).setAutoCancelDuration(3, TimeUnit.SECONDS).build()
        activeCamera.cameraControl.startFocusAndMetering(action)
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(slowScanRunnable)
        if (!finished.getAndSet(true)) {
            Log.i(TAG, "scan_ended engine=MLKIT attempt=$attemptId outcome=CANCELED total_ms=${SystemClock.elapsedRealtime() - attemptStartedMs}")
        }
        provider?.unbind(*listOfNotNull(previewUseCase, analysisUseCase).toTypedArray())
        runCatching { scanner.close() }
        analyzerExecutor.shutdownNow()
        super.onDestroy()
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private inner class ScannerGuideView : View(this@PairingQrCaptureActivity) {
        private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = 0xffd8ead6.toInt()
            style = Paint.Style.STROKE
            strokeWidth = dp(2).toFloat()
        }
        private val shade = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = 0x99000000.toInt()
            style = Paint.Style.FILL
        }

        override fun onDraw(canvas: Canvas) {
            super.onDraw(canvas)
            val side = minOf(width * 0.78f, height * 0.34f)
            val left = (width - side) / 2f
            val top = (height - side) / 2f
            val guide = RectF(left, top, left + side, top + side)
            canvas.drawRect(0f, 0f, width.toFloat(), top, shade)
            canvas.drawRect(0f, top, left, top + side, shade)
            canvas.drawRect(left + side, top, width.toFloat(), top + side, shade)
            canvas.drawRect(0f, top + side, width.toFloat(), height.toFloat(), shade)
            canvas.drawRoundRect(guide, dp(16).toFloat(), dp(16).toFloat(), border)
        }
    }

    companion object {
        const val EXTRA_QR_PAYLOAD = "com.jarvis.companion.extra.PAIRING_QR_PAYLOAD"
        const val EXTRA_USE_ZXING = "com.jarvis.companion.extra.PAIRING_USE_ZXING"
        private const val MAX_QR_CHARS = 8192
        private const val TAG = "ThozhaPairingQr"
    }
}
