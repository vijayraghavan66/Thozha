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
import com.google.mlkit.vision.barcode.ZoomSuggestionOptions
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** CameraX + bundled, on-device ML Kit QR path. No image or decoded text is persisted. */
class PairingQrCaptureActivity : ComponentActivity() {
    private var attemptId = ""
    private var attemptStartedMs = 0L
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
    private var firstQrCandidateMs: Long? = null
    private var analyzerConfigured = false
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
        attemptId = intent.getStringExtra(EXTRA_ATTEMPT_ID) ?: java.util.UUID.randomUUID().toString().take(8)
        attemptStartedMs = intent.getLongExtra(EXTRA_STARTED_MS, SystemClock.elapsedRealtime())
        window.statusBarColor = Color.BLACK
        window.navigationBarColor = Color.BLACK
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
                        analysisUseCase?.let(::configureAnalyzer)
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

    private fun configureAnalyzer(analysis: ImageAnalysis) {
        if (analyzerConfigured || finished.get()) return
        val boundCamera = camera ?: return
        analyzerConfigured = true
        val maxZoom = boundCamera.cameraInfo.zoomState.value?.maxZoomRatio ?: 1f
        val options = BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .enableAllPotentialBarcodes()
        if (maxZoom > 1f) {
            val zoom = ZoomSuggestionOptions.Builder(ZoomSuggestionOptions.ZoomCallback { ratio ->
                if (ratio <= 1f || ratio > maxZoom) {
                    false
                } else {
                    val requestedAt = SystemClock.elapsedRealtime()
                    boundCamera.cameraControl.setZoomRatio(ratio).addListener({
                        Log.i(TAG, "auto_zoom engine=MLKIT attempt=$attemptId ratio=$ratio elapsed_ms=${SystemClock.elapsedRealtime() - requestedAt}")
                    }, ContextCompat.getMainExecutor(this))
                    true
                }
            }).setMaxSupportedZoomRatio(maxZoom).build()
            options.setZoomSuggestionOptions(zoom)
        }
        scanner = BarcodeScanning.getClient(options.build())
        analysis.setAnalyzer(analyzerExecutor, ::analyzeFrame)
        Log.i(TAG, "decoder_ready engine=MLKIT attempt=$attemptId max_zoom=$maxZoom auto_zoom=${maxZoom > 1f} formats=QR")
    }

    private fun analyzeFrame(proxy: androidx.camera.core.ImageProxy) {
        val mediaImage = proxy.image
        if (mediaImage == null || finished.get()) {
            proxy.close()
            return
        }
        if (!firstFrameLogged) {
            firstFrameLogged = true
            val luma = sampleLuma(proxy)
            Log.i(
                TAG,
                "first_frame engine=MLKIT attempt=$attemptId since_start_ms=${SystemClock.elapsedRealtime() - attemptStartedMs} size=${proxy.width}x${proxy.height} rotation=${proxy.imageInfo.rotationDegrees} y_mean=${luma.mean} y_range=${luma.min}-${luma.max}",
            )
        }
        val input = InputImage.fromMediaImage(mediaImage, proxy.imageInfo.rotationDegrees)
        scanner.process(input)
            .addOnSuccessListener(analyzerExecutor) { barcodes ->
                val candidate = barcodes.firstOrNull { it.format == Barcode.FORMAT_QR_CODE && it.boundingBox != null }
                if (candidate != null && firstQrCandidateMs == null) {
                    val now = SystemClock.elapsedRealtime()
                    firstQrCandidateMs = now
                    val ready = previewReadyMs ?: attemptStartedMs
                    val box = candidate.boundingBox
                    Log.i(TAG, "qr_candidate engine=MLKIT attempt=$attemptId from_preview_ms=${now - ready} bbox=${box?.width() ?: 0}x${box?.height() ?: 0} decoded=${!candidate.rawValue.isNullOrBlank()}")
                    runOnUiThread { if (!finished.get()) status.text = "QR detected · hold steady" }
                }
                val detected = barcodes.asSequence()
                    .filter { it.format == Barcode.FORMAT_QR_CODE }
                    .mapNotNull { it.rawValue }
                    .firstOrNull { it.isNotBlank() && it.length <= MAX_QR_CHARS }
                if (detected != null && finished.compareAndSet(false, true)) {
                    val now = SystemClock.elapsedRealtime()
                    val ready = previewReadyMs ?: attemptStartedMs
                    val box = candidate?.boundingBox
                    Log.i(
                        TAG,
                        "decode_success engine=MLKIT attempt=$attemptId from_preview_ms=${now - ready} from_candidate_ms=${firstQrCandidateMs?.let { now - it } ?: -1} total_ms=${now - attemptStartedMs} chars=${detected.length} bbox=${box?.width() ?: 0}x${box?.height() ?: 0}",
                    )
                    runOnUiThread {
                        setResult(RESULT_OK, Intent().putExtra(EXTRA_QR_PAYLOAD, detected))
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

    private fun focusAt(x: Float, y: Float) {
        val activeCamera = camera ?: return
        if (previewView.width <= 0 || previewView.height <= 0) return
        val point = previewView.meteringPointFactory.createPoint(x, y)
        val action = FocusMeteringAction.Builder(
            point,
            FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE,
        ).setAutoCancelDuration(3, TimeUnit.SECONDS).build()
        val focusStartedMs = SystemClock.elapsedRealtime()
        val focusFuture = activeCamera.cameraControl.startFocusAndMetering(action)
        focusFuture.addListener({
            runCatching { focusFuture.get() }
                .onSuccess { Log.i(TAG, "autofocus_result engine=MLKIT attempt=$attemptId elapsed_ms=${SystemClock.elapsedRealtime() - focusStartedMs} success=${it.isFocusSuccessful}") }
                .onFailure { Log.w(TAG, "autofocus_error engine=MLKIT attempt=$attemptId type=${it.javaClass.simpleName}") }
        }, ContextCompat.getMainExecutor(this))
    }

    /** Log only coarse brightness statistics so a black feed can be distinguished from a decoder miss. */
    private fun sampleLuma(proxy: androidx.camera.core.ImageProxy): LumaStats {
        val buffer = proxy.planes.firstOrNull()?.buffer?.duplicate() ?: return LumaStats(0, 0, 0)
        if (!buffer.hasRemaining()) return LumaStats(0, 0, 0)
        val size = buffer.remaining()
        val targetSamples = 512
        val step = maxOf(1, size / targetSamples)
        var min = 255
        var max = 0
        var sum = 0L
        var count = 0
        while (buffer.hasRemaining()) {
            val value = buffer.get().toInt() and 0xff
            if (count % step == 0) {
                min = minOf(min, value)
                max = maxOf(max, value)
                sum += value
            }
            count++
        }
        val samples = (count + step - 1) / step
        return LumaStats((sum / maxOf(1, samples)).toInt(), min, max)
    }

    private data class LumaStats(val mean: Int, val min: Int, val max: Int)

    override fun onDestroy() {
        mainHandler.removeCallbacks(slowScanRunnable)
        if (!finished.getAndSet(true)) {
            Log.i(TAG, "scan_ended engine=MLKIT attempt=$attemptId outcome=CANCELED total_ms=${SystemClock.elapsedRealtime() - attemptStartedMs}")
        }
        provider?.unbind(*listOfNotNull(previewUseCase, analysisUseCase).toTypedArray())
        if (::scanner.isInitialized) runCatching { scanner.close() }
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
        const val EXTRA_ATTEMPT_ID = "com.jarvis.companion.extra.QR_ATTEMPT_ID"
        const val EXTRA_STARTED_MS = "com.jarvis.companion.extra.QR_STARTED_MS"
        private const val MAX_QR_CHARS = 8192
        private const val TAG = "ThozhaPairingQr"
    }
}
