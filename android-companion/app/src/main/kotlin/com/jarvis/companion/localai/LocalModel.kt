package com.jarvis.companion.localai

import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.util.Properties
import java.util.UUID

enum class LocalModelFormat { LITERT_LM }

/** Metadata needed to show an explicit model import choice. No model is downloaded by this catalog. */
data class LocalModelDescriptor(
    val id: String,
    val displayName: String,
    val fileName: String,
    val format: LocalModelFormat,
    val expectedBytes: Long,
    val minimumRamBytes: Long,
    val quantization: String,
    val sourcePage: String,
    val sourceRevision: String,
    val licenseName: String,
    val licenseUrl: String,
    val requiresLicenseAcceptance: Boolean = true,
    val expectedSha256: String? = null,
) {
    init {
        require(id.matches(Regex("^[a-z0-9._-]{1,64}$")))
        require(displayName.isNotBlank() && displayName.length <= 120)
        require(fileName.matches(Regex("^[A-Za-z0-9._-]{1,160}\\.litertlm$"))) { "model filename must be a plain .litertlm filename" }
        require(expectedBytes > 0 && expectedBytes <= MAX_MODEL_BYTES)
        require(minimumRamBytes > 0)
        require(quantization.isNotBlank() && quantization.length <= 80)
        require(sourcePage.startsWith("https://") && licenseUrl.startsWith("https://"))
        require(sourceRevision.matches(Regex("^[0-9a-f]{7,64}$")))
        require(expectedSha256 == null || expectedSha256.matches(Regex("^[0-9a-f]{64}$")))
    }

    companion object {
        const val MAX_MODEL_BYTES = 8L * 1024 * 1024 * 1024
    }
}

/** Candidate only; the gated 584 MB model is not bundled or fetched. Import requires explicit license consent. */
object LocalModelCatalog {
    val gemma3OneBInt4 = LocalModelDescriptor(
        id = "gemma3-1b-it-int4",
        displayName = "Gemma 3 1B IT (INT4)",
        fileName = "gemma3-1b-it-int4.litertlm",
        format = LocalModelFormat.LITERT_LM,
        expectedBytes = 584_417_280L,
        minimumRamBytes = 6L * 1024 * 1024 * 1024,
        quantization = "INT4",
        sourcePage = "https://huggingface.co/litert-community/Gemma3-1B-IT",
        sourceRevision = "42d538a932e8d5b12e6b3b455f5572560bd60b2c",
        licenseName = "Gemma Terms of Use",
        licenseUrl = "https://ai.google.dev/gemma/terms",
        requiresLicenseAcceptance = true,
        expectedSha256 = null,
    )
}

data class ModelStoragePreflight(
    val requiredBytes: Long,
    val availableBytes: Long,
    val enoughSpace: Boolean,
    val licenseAcceptanceRequired: Boolean,
)

data class InstalledLocalModel(
    val descriptor: LocalModelDescriptor,
    val file: File,
    val sizeBytes: Long,
    val sha256: String,
    val licenseAccepted: Boolean,
)

enum class LocalModelErrorCode {
    LICENSE_NOT_ACCEPTED,
    INSUFFICIENT_STORAGE,
    MODEL_TOO_LARGE,
    MODEL_SIZE_MISMATCH,
    MODEL_HASH_MISMATCH,
    MODEL_MISSING,
    MODEL_OUTSIDE_PRIVATE_STORAGE,
    IMPORT_CANCELLED,
}

class LocalModelException(val code: LocalModelErrorCode, message: String) : Exception(message)

/**
 * App-private storage only. Import is explicitly initiated by a caller with a user-selected stream;
 * this class has no URL, HTTP or automatic download API.
 */
class LocalModelStore(private val appFilesDir: File) {
    private val root = File(appFilesDir, "local-models")

    fun preflight(descriptor: LocalModelDescriptor): ModelStoragePreflight {
        val rootCanonical = ensurePrivateRoot()
        val available = rootCanonical.usableSpace.coerceAtLeast(0L)
        return ModelStoragePreflight(
            requiredBytes = descriptor.expectedBytes,
            availableBytes = available,
            enoughSpace = available >= descriptor.expectedBytes,
            licenseAcceptanceRequired = descriptor.requiresLicenseAcceptance,
        )
    }

    @Synchronized fun importSelectedModel(
        descriptor: LocalModelDescriptor,
        source: InputStream,
        licenseAccepted: Boolean,
        isCancelled: () -> Boolean = { false },
    ): InstalledLocalModel {
        if (descriptor.requiresLicenseAcceptance && !licenseAccepted) {
            throw LocalModelException(LocalModelErrorCode.LICENSE_NOT_ACCEPTED, "Model terms must be accepted before importing this model.")
        }
        val privateRoot = ensurePrivateRoot()
        if (descriptor.expectedBytes > LocalModelDescriptor.MAX_MODEL_BYTES) {
            throw LocalModelException(LocalModelErrorCode.MODEL_TOO_LARGE, "Model exceeds the configured import limit.")
        }
        if (privateRoot.usableSpace < descriptor.expectedBytes) {
            throw LocalModelException(LocalModelErrorCode.INSUFFICIENT_STORAGE, "Not enough app-private storage for this model.")
        }
        val destination = File(privateRoot, descriptor.fileName)
        ensureChild(privateRoot, destination)
        val partial = File(privateRoot, ".${descriptor.fileName}.${UUID.randomUUID()}.part")
        ensureChild(privateRoot, partial)
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        try {
            FileOutputStream(partial).use { output ->
                val buffer = ByteArray(COPY_BUFFER_BYTES)
                while (true) {
                    if (isCancelled()) throw LocalModelException(LocalModelErrorCode.IMPORT_CANCELLED, "Model import was cancelled.")
                    val count = source.read(buffer)
                    if (count < 0) break
                    total += count
                    if (total > descriptor.expectedBytes || total > LocalModelDescriptor.MAX_MODEL_BYTES) {
                        throw LocalModelException(LocalModelErrorCode.MODEL_TOO_LARGE, "Selected file exceeds its declared model size.")
                    }
                    digest.update(buffer, 0, count)
                    output.write(buffer, 0, count)
                }
                output.flush()
                output.fd.sync()
            }
            if (total != descriptor.expectedBytes) {
                throw LocalModelException(LocalModelErrorCode.MODEL_SIZE_MISMATCH, "Selected file size does not match the model catalog entry.")
            }
            val hash = digest.digest().joinToString("") { "%02x".format(it) }
            if (descriptor.expectedSha256 != null && !hash.equals(descriptor.expectedSha256, ignoreCase = true)) {
                throw LocalModelException(LocalModelErrorCode.MODEL_HASH_MISMATCH, "Selected model did not match its published checksum.")
            }
            if (destination.exists()) {
                val existing = verifyInstalled(descriptor)
                if (existing.sha256 != hash) {
                    throw LocalModelException(LocalModelErrorCode.MODEL_HASH_MISMATCH, "A different file already occupies this model slot.")
                }
                partial.delete()
                return existing
            }
            if (!partial.renameTo(destination)) throw LocalModelException(LocalModelErrorCode.MODEL_MISSING, "Could not finalize the model import.")
            writeInstallManifest(privateRoot, descriptor, hash, licenseAccepted)
            return InstalledLocalModel(descriptor, destination, total, hash, licenseAccepted)
        } finally {
            if (partial.exists()) partial.delete()
        }
    }

    @Synchronized fun verifyInstalled(descriptor: LocalModelDescriptor): InstalledLocalModel {
        val privateRoot = ensurePrivateRoot()
        val model = File(privateRoot, descriptor.fileName)
        ensureChild(privateRoot, model)
        if (!model.isFile || Files.isSymbolicLink(model.toPath())) {
            throw LocalModelException(LocalModelErrorCode.MODEL_MISSING, "Model file is not installed in app-private storage.")
        }
        if (model.length() != descriptor.expectedBytes) {
            throw LocalModelException(LocalModelErrorCode.MODEL_SIZE_MISMATCH, "Installed model size does not match its catalog entry.")
        }
        val hash = FileInputStream(model).use { input ->
            val digest = MessageDigest.getInstance("SHA-256")
            val buffer = ByteArray(COPY_BUFFER_BYTES)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
            digest.digest().joinToString("") { "%02x".format(it) }
        }
        if (descriptor.expectedSha256 != null && !hash.equals(descriptor.expectedSha256, ignoreCase = true)) {
            throw LocalModelException(LocalModelErrorCode.MODEL_HASH_MISMATCH, "Installed model checksum does not match the catalog entry.")
        }
        val manifest = Properties()
        val manifestPath = manifestFile(privateRoot, descriptor)
        if (!manifestPath.isFile || Files.isSymbolicLink(manifestPath.toPath())) {
            throw LocalModelException(LocalModelErrorCode.MODEL_HASH_MISMATCH, "Installed model integrity metadata is missing.")
        }
        FileInputStream(manifestPath).use(manifest::load)
        val recordedHash = manifest.getProperty("sha256")
        val accepted = manifest.getProperty("license_accepted") == "true"
        if (manifest.getProperty("model_id") != descriptor.id ||
            manifest.getProperty("source_revision") != descriptor.sourceRevision ||
            manifest.getProperty("size_bytes") != descriptor.expectedBytes.toString() ||
            !hash.equals(recordedHash, ignoreCase = true)
        ) {
            throw LocalModelException(LocalModelErrorCode.MODEL_HASH_MISMATCH, "Installed model does not match its private import record.")
        }
        if (descriptor.requiresLicenseAcceptance && !accepted) {
            throw LocalModelException(LocalModelErrorCode.LICENSE_NOT_ACCEPTED, "Model terms were not accepted for this installation.")
        }
        return InstalledLocalModel(descriptor, model, model.length(), hash, accepted)
    }

    private fun ensurePrivateRoot(): File {
        val appRoot = appFilesDir.canonicalFile
        if (!appRoot.exists() && !appRoot.mkdirs()) throw LocalModelException(LocalModelErrorCode.MODEL_OUTSIDE_PRIVATE_STORAGE, "Could not create app-private model storage.")
        val canonical = root.canonicalFile
        if (!canonical.path.startsWith(appRoot.path + File.separator)) {
            throw LocalModelException(LocalModelErrorCode.MODEL_OUTSIDE_PRIVATE_STORAGE, "Model storage escaped app-private files.")
        }
        if (!canonical.exists() && !canonical.mkdirs()) throw LocalModelException(LocalModelErrorCode.MODEL_OUTSIDE_PRIVATE_STORAGE, "Could not create app-private model storage.")
        return canonical
    }

    private fun ensureChild(parent: File, child: File) {
        if (!child.canonicalFile.path.startsWith(parent.canonicalPath + File.separator)) {
            throw LocalModelException(LocalModelErrorCode.MODEL_OUTSIDE_PRIVATE_STORAGE, "Model path escaped app-private storage.")
        }
    }

    private fun writeInstallManifest(parent: File, descriptor: LocalModelDescriptor, hash: String, licenseAccepted: Boolean) {
        val manifest = manifestFile(parent, descriptor)
        val temporary = File(parent, ".${descriptor.id}.${UUID.randomUUID()}.manifest.part")
        try {
            val properties = Properties().apply {
                setProperty("model_id", descriptor.id)
                setProperty("source_revision", descriptor.sourceRevision)
                setProperty("size_bytes", descriptor.expectedBytes.toString())
                setProperty("sha256", hash)
                setProperty("license_accepted", licenseAccepted.toString())
            }
            FileOutputStream(temporary).use { output -> properties.store(output, "Jarvis local model install receipt") }
            if (!temporary.renameTo(manifest)) throw LocalModelException(LocalModelErrorCode.MODEL_MISSING, "Could not finalize model integrity metadata.")
        } finally {
            if (temporary.exists()) temporary.delete()
        }
    }

    private fun manifestFile(parent: File, descriptor: LocalModelDescriptor) = File(parent, "${descriptor.id}.properties")

    private companion object { const val COPY_BUFFER_BYTES = 64 * 1024 }
}
