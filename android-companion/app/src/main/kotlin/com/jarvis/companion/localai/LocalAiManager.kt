package com.jarvis.companion.localai

import android.app.ActivityManager
import android.content.Context
import java.io.InputStream

/** Process-scoped owner for the app-private LiteRT model and its non-tool-capable inference backend. */
class LocalAiManager private constructor(context: Context) {
    private val app = context.applicationContext
    private val modelStore = LocalModelStore(app.filesDir)
    private val runtime = LocalAiRuntime(modelStore, LiteRtLmBackend())
    private val memory = app.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager

    fun status(): LocalAiSnapshot = runtime.status()

    fun preflight(): ModelStoragePreflight {
        val result = modelStore.preflight(LocalModelCatalog.gemma3OneBInt4)
        return result.copy(enoughSpace = result.enoughSpace && hasRequiredMemory())
    }

    @Synchronized fun importSelectedModel(source: InputStream, licenseAccepted: Boolean): InstalledLocalModel {
        if (!hasRequiredMemory()) {
            throw LocalAiException(LocalAiErrorCode.MODEL_INVALID, "This device does not meet the model's published 6 GiB minimum memory requirement.")
        }
        val installed = modelStore.importSelectedModel(
            LocalModelCatalog.gemma3OneBInt4,
            source,
            licenseAccepted = licenseAccepted,
        )
        loadInstalledModel()
        return installed
    }

    @Synchronized fun loadInstalledModel(): LocalAiSnapshot {
        if (!hasRequiredMemory()) {
            throw LocalAiException(LocalAiErrorCode.MODEL_INVALID, "This device does not meet the model's published 6 GiB minimum memory requirement.")
        }
        return runtime.loadModel(LocalModelCatalog.gemma3OneBInt4)
    }

    fun generate(prompt: String, onText: (String) -> Unit = {}): String = runtime.generate(prompt, onText)
    fun cancel() = runtime.cancel()
    fun unload() = runtime.unload()

    private fun hasRequiredMemory(): Boolean {
        val minimumRamBytes = LocalModelCatalog.gemma3OneBInt4.minimumRamBytes ?: return true
        val info = ActivityManager.MemoryInfo()
        memory.getMemoryInfo(info)
        return info.totalMem >= minimumRamBytes
    }

    companion object {
        @Volatile private var instance: LocalAiManager? = null

        fun get(context: Context): LocalAiManager = instance ?: synchronized(this) {
            instance ?: LocalAiManager(context).also { instance = it }
        }
    }
}
