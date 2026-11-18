package com.jarvis.companion.localai

import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Conversation
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import java.util.concurrent.atomic.AtomicReference

/** Real LiteRT-LM adapter. It has no network, shell or tool-execution capability. */
class LiteRtLmBackend : LocalInferenceBackend {
    override val runtimeId = "litert-lm-0.16.1-cpu"
    override val supportedFormats = setOf(LocalModelFormat.LITERT_LM)
    private val lock = Any()
    private val activeGeneration = AtomicReference<Job?>(null)
    private var engine: Engine? = null
    private var conversation: Conversation? = null
    private var closed = false

    override fun load(model: InstalledLocalModel) {
        synchronized(lock) {
            if (closed) throw LocalAiException(LocalAiErrorCode.CLOSED, "Local inference backend is closed.")
        }
        unload()
        val loadedEngine = try {
            Engine(EngineConfig(modelPath = model.file.absolutePath, backend = Backend.CPU())).also { it.initialize() }
        } catch (failure: Exception) {
            throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "LiteRT-LM could not load the selected model.", failure)
        }
        val loadedConversation = try {
            loadedEngine.createConversation()
        } catch (failure: Exception) {
            runCatching { loadedEngine.close() }
            throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "LiteRT-LM could not create a conversation.", failure)
        }
        synchronized(lock) {
            if (closed) {
                runCatching { loadedConversation.close() }
                runCatching { loadedEngine.close() }
                throw LocalAiException(LocalAiErrorCode.CLOSED, "Local inference backend closed during model load.")
            }
            engine = loadedEngine
            conversation = loadedConversation
        }
    }

    override fun generate(prompt: String, cancellation: LocalCancellationToken, onText: (String) -> Unit): String {
        val activeConversation = synchronized(lock) {
            if (closed) throw LocalAiException(LocalAiErrorCode.CLOSED, "Local inference backend is closed.")
            conversation ?: run {
                val loadedEngine = engine ?: throw LocalAiException(LocalAiErrorCode.MODEL_NOT_INSTALLED, "No local model is loaded.")
                loadedEngine.createConversation().also { conversation = it }
            }
        }
        val output = StringBuilder()
        return try {
            runBlocking(Dispatchers.IO) {
                val generation = launch {
                    activeConversation.sendMessageAsync(prompt).collect { message ->
                        cancellation.throwIfCancelled()
                        val chunk = message.toString()
                        if (chunk.isNotEmpty()) {
                            output.append(chunk)
                            onText(chunk)
                        }
                    }
                }
                activeGeneration.set(generation)
                try {
                    generation.join()
                    cancellation.throwIfCancelled()
                    output.toString()
                } finally {
                    activeGeneration.compareAndSet(generation, null)
                }
            }
        } catch (failure: CancellationException) {
            throw LocalAiException(LocalAiErrorCode.CANCELLED, "Local inference was cancelled.", failure)
        } catch (failure: LocalAiException) {
            throw failure
        } catch (failure: Exception) {
            throw LocalAiException(LocalAiErrorCode.INFERENCE_FAILED, "LiteRT-LM generation failed.", failure)
        }
    }

    override fun cancel() {
        activeGeneration.getAndSet(null)?.cancel(CancellationException("Local inference cancelled"))
        val oldConversation = synchronized(lock) {
            val previous = conversation
            conversation = null
            previous
        }
        runCatching { oldConversation?.close() }
    }

    override fun unload() {
        cancel()
        val oldEngine = synchronized(lock) {
            val previous = engine
            engine = null
            previous
        }
        runCatching { oldEngine?.close() }
    }

    override fun close() {
        synchronized(lock) { closed = true }
        unload()
    }
}
