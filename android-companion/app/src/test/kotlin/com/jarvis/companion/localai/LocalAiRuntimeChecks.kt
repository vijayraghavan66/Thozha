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
        modelCatalogAndAcquisitionRequireExplicitSafeConsent()
        runtimeRejectsUnsupportedModelFormatBeforeLoading()
        println("Local AI runtime checks passed: 7 cases; fake backend used only for lifecycle tests")
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

    private fun modelCatalogAndAcquisitionRequireExplicitSafeConsent() {
        val model = LocalModelCatalog.qwen3ZeroPointSixBQ4
        check(model.format == LocalModelFormat.GGUF)
        check(model.expectedBytes == 449_839_104L)
        check(model.expectedSha256 == "da2572f16c06133561ce56accaa822216f2391ef4d37fba427801cd6736417d4")
        check(model.licenseName == "Apache-2.0" && !model.requiresLicenseAcceptance)
        check(model.minimumRamBytes == null)
        val noExplicitStart = runCatching {
            LocalModelAcquisitionPolicy.authorizeCatalogDownload(
                model, model.expectedBytes,
                ModelAcquisitionConsent(userInitiated = false, licenseAccepted = true, networkMetered = false, allowMeteredNetwork = false),
            )
        }.exceptionOrNull()
        check((noExplicitStart as? ModelAcquisitionException)?.code == ModelAcquisitionError.NOT_USER_INITIATED)
        val metered = runCatching {
            LocalModelAcquisitionPolicy.authorizeCatalogDownload(
                model, model.expectedBytes,
                ModelAcquisitionConsent(userInitiated = true, licenseAccepted = true, networkMetered = true, allowMeteredNetwork = false),
            )
        }.exceptionOrNull()
        check((metered as? ModelAcquisitionException)?.code == ModelAcquisitionError.METERED_NETWORK_NOT_ALLOWED)
        val noSpace = runCatching {
            LocalModelAcquisitionPolicy.authorizeCatalogDownload(
                model, model.expectedBytes - 1,
                ModelAcquisitionConsent(userInitiated = true, licenseAccepted = true, networkMetered = false, allowMeteredNetwork = false),
            )
        }.exceptionOrNull()
        check((noSpace as? ModelAcquisitionException)?.code == ModelAcquisitionError.INSUFFICIENT_STORAGE)
        val permitted = LocalModelAcquisitionPolicy.authorizeCatalogDownload(
            model, model.expectedBytes,
            ModelAcquisitionConsent(userInitiated = true, licenseAccepted = true, networkMetered = false, allowMeteredNetwork = false),
        )
        check(permitted.expectedSha256 == model.expectedSha256 && permitted.requiredBytes == model.expectedBytes)
        check(permitted.downloadUrl == "https://huggingface.co/ggml-org/Qwen3-0.6B-GGUF/resolve/afe8ad3/Qwen3-0.6B-Q4_0.gguf")

        val gemma = LocalModelCatalog.gemma3OneBInt4
        val noLicense = runCatching {
            LocalModelAcquisitionPolicy.authorizeCatalogDownload(
                gemma, gemma.expectedBytes,
                ModelAcquisitionConsent(userInitiated = true, licenseAccepted = false, networkMetered = false, allowMeteredNetwork = false),
            )
        }.exceptionOrNull()
        check((noLicense as? ModelAcquisitionException)?.code == ModelAcquisitionError.LICENSE_NOT_ACCEPTED)
        val unverifiedGemma = runCatching {
            LocalModelAcquisitionPolicy.authorizeCatalogDownload(
                gemma, gemma.expectedBytes,
                ModelAcquisitionConsent(userInitiated = true, licenseAccepted = true, networkMetered = false, allowMeteredNetwork = false),
            )
        }.exceptionOrNull()
        check((unverifiedGemma as? ModelAcquisitionException)?.code == ModelAcquisitionError.UNVERIFIED_SOURCE)
    }

    private fun runtimeRejectsUnsupportedModelFormatBeforeLoading() {
        val dir = Files.createTempDirectory("jarvis-local-ai-format").toFile()
        val gguf = LocalModelCatalog.qwen3ZeroPointSixBQ4.copy(
            expectedBytes = 3,
            expectedSha256 = MessageDigest.getInstance("SHA-256").digest("abc".toByteArray())
                .joinToString("") { "%02x".format(it) },
        )
        try {
            val store = LocalModelStore(dir)
            store.importSelectedModel(gguf, ByteArrayInputStream("abc".toByteArray()), licenseAccepted = false)
            val backend = TestBackend()
            val runtime = LocalAiRuntime(store, backend)
            val failure = runCatching { runtime.loadModel(gguf) }.exceptionOrNull()
            check((failure as? LocalAiException)?.code == LocalAiErrorCode.UNSUPPORTED_MODEL_FORMAT)
            check(backend.loadCount == 0)
            runtime.close()
        } finally { dir.deleteRecursively() }
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
        override val supportedFormats = setOf(LocalModelFormat.LITERT_LM)
        var lastPrompt: String? = null
        var cancelOnGenerate = false
        var unloadCount = 0
        var loadCount = 0
        private val canceled = AtomicBoolean(false)
        override fun load(model: InstalledLocalModel) { loadCount++; canceled.set(false) }
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
