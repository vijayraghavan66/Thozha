package com.jarvis.companion.localai

import java.io.Closeable
import java.util.concurrent.CancellationException
import java.util.concurrent.atomic.AtomicBoolean

enum class LocalAiPhase { NOT_READY, LOADING, READY, GENERATING, CANCELLING, CANCELLED, FAILED, CLOSED }

data class LocalAiSnapshot(
    val phase: LocalAiPhase,
    val modelId: String? = null,
    val error: LocalAiErrorCode? = null,
)

enum class LocalAiErrorCode {
    BACKEND_UNAVAILABLE,
    MODEL_NOT_INSTALLED,
    MODEL_INVALID,
    LICENSE_REQUIRED,
    INVALID_PROMPT,
    INFERENCE_FAILED,
    CANCELLED,
    CLOSED,
}

class LocalAiException(val code: LocalAiErrorCode, message: String, cause: Throwable? = null) : Exception(message, cause)

class LocalCancellationToken internal constructor() {
    private val cancelled = AtomicBoolean(false)
    val isCancelled: Boolean get() = cancelled.get()
    fun cancel() { cancelled.set(true) }
    fun throwIfCancelled() {
        if (isCancelled) throw CancellationException("Local inference was cancelled.")
    }
}

/** No tool API is exposed here: local model output is plain text and cannot approve or execute actions. */
interface LocalInferenceBackend : Closeable {
    val runtimeId: String
    fun load(model: InstalledLocalModel)
    fun generate(prompt: String, cancellation: LocalCancellationToken, onText: (String) -> Unit = {}): String
    fun cancel()
    fun unload()
    override fun close() = unload()
}

/**
 * Explicit local-only inference facade. It never falls back to the network and it never fabricates
 * an answer. The caller must install a verified model and supply a real backend adapter.
 * Invoke load/generate from a background executor; engine initialization and inference may block.
 */
class LocalAiRuntime(
    private val modelStore: LocalModelStore,
    private val backend: LocalInferenceBackend?,
) : Closeable {
    private val lock = Any()
    @Volatile private var snapshot = LocalAiSnapshot(LocalAiPhase.NOT_READY)
    private var loadedModel: LocalModelDescriptor? = null
    private var activeCancellation: LocalCancellationToken? = null
    private var closed = false

    fun status(): LocalAiSnapshot = snapshot

    fun loadModel(descriptor: LocalModelDescriptor): LocalAiSnapshot {
        val engine = backend ?: throw LocalAiException(LocalAiErrorCode.BACKEND_UNAVAILABLE, "No on-device inference backend is bundled in this build.")
        synchronized(lock) {
            ensureOpen()
            if (snapshot.phase == LocalAiPhase.GENERATING || snapshot.phase == LocalAiPhase.CANCELLING) {
                throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "Stop the current local inference before loading another model.")
            }
            snapshot = LocalAiSnapshot(LocalAiPhase.LOADING, descriptor.id)
        }
        try {
            val model = modelStore.verifyInstalled(descriptor)
            engine.unload()
            engine.load(model)
            synchronized(lock) {
                ensureOpen()
                loadedModel = descriptor
                snapshot = LocalAiSnapshot(LocalAiPhase.READY, descriptor.id)
            }
            return snapshot
        } catch (failure: LocalModelException) {
            val mapped = when (failure.code) {
                LocalModelErrorCode.LICENSE_NOT_ACCEPTED -> LocalAiErrorCode.LICENSE_REQUIRED
                LocalModelErrorCode.MODEL_MISSING -> LocalAiErrorCode.MODEL_NOT_INSTALLED
                else -> LocalAiErrorCode.MODEL_INVALID
            }
            synchronized(lock) { snapshot = LocalAiSnapshot(LocalAiPhase.FAILED, descriptor.id, mapped) }
            throw LocalAiException(mapped, failure.message ?: "The selected local model is not ready.", failure)
        } catch (failure: Exception) {
            synchronized(lock) { snapshot = LocalAiSnapshot(LocalAiPhase.FAILED, descriptor.id, LocalAiErrorCode.INFERENCE_FAILED) }
            throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "The local runtime could not load this model.", failure)
        }
    }

    fun generate(prompt: String, onText: (String) -> Unit = {}): String {
        val cleaned = prompt.trim()
        if (cleaned.isEmpty() || cleaned.toByteArray(Charsets.UTF_8).size > MAX_PROMPT_BYTES) {
            throw LocalAiException(LocalAiErrorCode.INVALID_PROMPT, "Local requests must be 1–4096 UTF-8 bytes.")
        }
        val engine = backend ?: throw LocalAiException(LocalAiErrorCode.BACKEND_UNAVAILABLE, "No on-device inference backend is bundled in this build.")
        val modelId: String
        val token = LocalCancellationToken()
        synchronized(lock) {
            ensureOpen()
            modelId = loadedModel?.id ?: throw LocalAiException(LocalAiErrorCode.MODEL_NOT_INSTALLED, "Choose and install a local model first.")
            if (snapshot.phase == LocalAiPhase.GENERATING || snapshot.phase == LocalAiPhase.CANCELLING) {
                throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "Local inference is already running.")
            }
            activeCancellation = token
            snapshot = LocalAiSnapshot(LocalAiPhase.GENERATING, modelId)
        }
        return try {
            val answer = engine.generate(cleaned, token, onText).trim()
            token.throwIfCancelled()
            synchronized(lock) { snapshot = LocalAiSnapshot(LocalAiPhase.READY, modelId) }
            answer
        } catch (failure: CancellationException) {
            synchronized(lock) {
                if (!closed) snapshot = LocalAiSnapshot(LocalAiPhase.CANCELLED, modelId, LocalAiErrorCode.CANCELLED)
            }
            throw LocalAiException(LocalAiErrorCode.CANCELLED, "Local inference was cancelled.", failure)
        } catch (failure: LocalAiException) {
            synchronized(lock) {
                if (!closed) {
                    snapshot = if (failure.code == LocalAiErrorCode.CANCELLED) {
                        LocalAiSnapshot(LocalAiPhase.CANCELLED, modelId, LocalAiErrorCode.CANCELLED)
                    } else LocalAiSnapshot(LocalAiPhase.FAILED, modelId, failure.code)
                }
            }
            throw failure
        } catch (failure: Exception) {
            synchronized(lock) { if (!closed) snapshot = LocalAiSnapshot(LocalAiPhase.FAILED, modelId, LocalAiErrorCode.INFERENCE_FAILED) }
            throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "Local inference failed.", failure)
        } finally {
            synchronized(lock) { activeCancellation = null }
        }
    }

    fun cancel() {
        val engine = backend ?: return
        synchronized(lock) {
            val token = activeCancellation ?: return
            token.cancel()
            snapshot = LocalAiSnapshot(LocalAiPhase.CANCELLING, loadedModel?.id, LocalAiErrorCode.CANCELLED)
        }
        runCatching { engine.cancel() }
    }

    fun unload() {
        val engine = backend ?: return
        synchronized(lock) {
            if (snapshot.phase == LocalAiPhase.GENERATING || snapshot.phase == LocalAiPhase.CANCELLING) {
                throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "Cancel local inference before unloading its model.")
            }
            engine.unload()
            loadedModel = null
            snapshot = LocalAiSnapshot(LocalAiPhase.NOT_READY)
        }
    }

    override fun close() {
        val engine = backend
        synchronized(lock) {
            if (closed) return
            closed = true
            activeCancellation?.cancel()
            loadedModel = null
            snapshot = LocalAiSnapshot(LocalAiPhase.CLOSED)
        }
        runCatching { engine?.cancel() }
        runCatching { engine?.close() }
    }

    private fun ensureOpen() {
        if (closed) throw LocalAiException(LocalAiErrorCode.CLOSED, "Local runtime is closed.")
    }

    companion object { const val MAX_PROMPT_BYTES = 4096 }
}
