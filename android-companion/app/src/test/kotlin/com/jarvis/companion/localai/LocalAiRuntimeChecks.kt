package com.jarvis.companion.localai

import java.io.ByteArrayInputStream
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean

object LocalAiRuntimeChecks {
    @JvmStatic fun main(args: Array<String>) {
        explicitImportRequiresConsentAndVerifiesPrivateArtifact()
        tamperedModelIsRejected()
        runtimeNeverFabricatesAnAnswerWithoutABackend()
        runtimeLifecycleAndCancellationUseInjectedBackend()
        routingIsExplicitAndDeterministic()
        println("Local AI runtime checks passed: 5 cases; fake backend used only for lifecycle tests")
    }

    private fun explicitImportRequiresConsentAndVerifiesPrivateArtifact() {
        val dir = Files.createTempDirectory("jarvis-local-ai-store").toFile()
        try {
            val bytes = "abc".toByteArray()
            val descriptor = descriptor(bytes)
            val store = LocalModelStore(dir)
            val preflight = store.preflight(descriptor)
            check(preflight.requiredBytes == bytes.size.toLong() && preflight.enoughSpace)
            check(preflight.licenseAcceptanceRequired)
            val noConsent = runCatching {
                store.importSelectedModel(descriptor, ByteArrayInputStream(bytes), licenseAccepted = false)
            }.exceptionOrNull()
            check((noConsent as? LocalModelException)?.code == LocalModelErrorCode.LICENSE_NOT_ACCEPTED)

            val installed = store.importSelectedModel(descriptor, ByteArrayInputStream(bytes), licenseAccepted = true)
            check(installed.file.readBytes().contentEquals(bytes))
            check(installed.file.canonicalPath.startsWith(File(dir, "local-models").canonicalPath + File.separator))
            check(installed.licenseAccepted && installed.sha256 == descriptor.expectedSha256)
            check(store.verifyInstalled(descriptor).sha256 == installed.sha256)
        } finally { dir.deleteRecursively() }
    }

    private fun tamperedModelIsRejected() {
        val dir = Files.createTempDirectory("jarvis-local-ai-tamper").toFile()
        try {
            val descriptor = descriptor("abc".toByteArray())
            val store = LocalModelStore(dir)
            val installed = store.importSelectedModel(descriptor, ByteArrayInputStream("abc".toByteArray()), licenseAccepted = true)
            installed.file.writeBytes("xyz".toByteArray())
            val failure = runCatching { store.verifyInstalled(descriptor) }.exceptionOrNull()
            check((failure as? LocalModelException)?.code == LocalModelErrorCode.MODEL_HASH_MISMATCH)
        } finally { dir.deleteRecursively() }
    }

    private fun runtimeNeverFabricatesAnAnswerWithoutABackend() {
        val dir = Files.createTempDirectory("jarvis-local-ai-no-backend").toFile()
        try {
            val runtime = LocalAiRuntime(LocalModelStore(dir), backend = null)
            val failure = runCatching { runtime.loadModel(descriptor("abc".toByteArray())) }.exceptionOrNull()
            check((failure as? LocalAiException)?.code == LocalAiErrorCode.BACKEND_UNAVAILABLE)
            check(runtime.status().phase == LocalAiPhase.NOT_READY)
        } finally { dir.deleteRecursively() }
    }

    private fun runtimeLifecycleAndCancellationUseInjectedBackend() {
        val dir = Files.createTempDirectory("jarvis-local-ai-lifecycle").toFile()
        val backend = TestBackend()
        try {
            val descriptor = descriptor("abc".toByteArray(), requiresLicenseAcceptance = false)
            val store = LocalModelStore(dir)
            store.importSelectedModel(descriptor, ByteArrayInputStream("abc".toByteArray()), licenseAccepted = false)
            val runtime = LocalAiRuntime(store, backend)
            runtime.loadModel(descriptor)
            check(runtime.status().phase == LocalAiPhase.READY)
            check(runtime.generate("safe local test prompt") == "test answer")
            check(backend.lastPrompt == "safe local test prompt")
            val emitted = mutableListOf<String>()
            backend.cancelOnGenerate = true
            val cancellation = runCatching { runtime.generate("cancel this", emitted::add) }.exceptionOrNull()
            check((cancellation as? LocalAiException)?.code == LocalAiErrorCode.CANCELLED)
            check(runtime.status().phase == LocalAiPhase.CANCELLED)
            runtime.unload()
            check(runtime.status().phase == LocalAiPhase.NOT_READY)
            check(backend.unloadCount >= 1)
            runtime.close()
            check(runtime.status().phase == LocalAiPhase.CLOSED)
        } finally { dir.deleteRecursively() }
    }

    private fun routingIsExplicitAndDeterministic() {
        val id = "routing-test-request-0001"
        val simple = "What is Java?"
        val complex = "Create a project and write Kotlin code, then run tests."
        check(RequestRoutingPolicy.route(id, simple, LocalAiRequestMode.AUTO, true) is RequestRoute.Local)
        check(RequestRoutingPolicy.route(id, simple, LocalAiRequestMode.AUTO, false) is RequestRoute.Mac)
        check(RequestRoutingPolicy.route(id, complex, LocalAiRequestMode.AUTO, true) is RequestRoute.Mac)
        val unavailable = RequestRoutingPolicy.route(id, simple, LocalAiRequestMode.LOCAL_ONLY, false)
        check((unavailable as? RequestRoute.Rejected)?.code == RouteError.LOCAL_MODEL_UNAVAILABLE)
        val needsMac = RequestRoutingPolicy.route(id, complex, LocalAiRequestMode.LOCAL_ONLY, true)
        check((needsMac as? RequestRoute.Rejected)?.code == RouteError.LOCAL_CANNOT_HANDLE)
        check(RequestRoutingPolicy.route(id, simple, LocalAiRequestMode.MAC_ONLY, true) is RequestRoute.Mac)
        check(RequestRoutingPolicy.route(id, simple, LocalAiRequestMode.LOCAL_ONLY, false, isExactFastCommand = true) is RequestRoute.FastCommandUpstream)
        check((RequestRoutingPolicy.route("bad", simple, LocalAiRequestMode.AUTO, true) as? RequestRoute.Rejected)?.code == RouteError.INVALID_REQUEST)
        check(RequestRoutingPolicy.routeAfterLocalFailure(id, LocalAiRequestMode.AUTO, partialOutputProduced = false) is RequestRoute.Mac)
        check((RequestRoutingPolicy.routeAfterLocalFailure(id, LocalAiRequestMode.AUTO, partialOutputProduced = true) as? RequestRoute.Rejected)?.code == RouteError.LOCAL_INFERENCE_FAILED)
        check((RequestRoutingPolicy.routeAfterLocalFailure(id, LocalAiRequestMode.LOCAL_ONLY, partialOutputProduced = false) as? RequestRoute.Rejected)?.code == RouteError.LOCAL_INFERENCE_FAILED)
    }

    private fun descriptor(bytes: ByteArray, requiresLicenseAcceptance: Boolean = true): LocalModelDescriptor {
        val hash = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        return LocalModelDescriptor(
            id = "test-local-model",
            displayName = "Test model",
            fileName = "test-model.litertlm",
            format = LocalModelFormat.LITERT_LM,
            expectedBytes = bytes.size.toLong(),
            minimumRamBytes = 1,
            quantization = "test-int4",
            sourcePage = "https://models.example/test",
            sourceRevision = "a".repeat(40),
            licenseName = "Test terms",
            licenseUrl = "https://models.example/license",
            requiresLicenseAcceptance = requiresLicenseAcceptance,
            expectedSha256 = hash,
        )
    }

    private class TestBackend : LocalInferenceBackend {
        override val runtimeId = "test-only-fake"
        var lastPrompt: String? = null
        var cancelOnGenerate = false
        var unloadCount = 0
        private val canceled = AtomicBoolean(false)
        override fun load(model: InstalledLocalModel) { canceled.set(false) }
        override fun generate(prompt: String, cancellation: LocalCancellationToken, onText: (String) -> Unit): String {
            lastPrompt = prompt
            if (cancelOnGenerate) {
                cancellation.cancel()
                cancellation.throwIfCancelled()
            }
            onText("test answer")
            return "test answer"
        }
        override fun cancel() { canceled.set(true) }
        override fun unload() { unloadCount++; canceled.set(true) }
        override fun close() { unload() }
    }
}
