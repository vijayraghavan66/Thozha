package com.jarvis.companion.workspace

import android.content.Context
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.zip.ZipInputStream

class StaticPreviewStore private constructor(private val root: File) {
    constructor(context: Context) : this(File(context.filesDir, "workspace/previews").apply { mkdirs() })
    internal constructor(root: File, testRoot: Unit = Unit) : this(root.apply { mkdirs() })
    data class Preview(val taskId: String, val directory: File, val indexHtml: File, val files: List<String>, val archiveSha256: String)

    fun install(taskId: String, archive: ByteArray, expectedSha256: String): Preview {
        require(taskId.matches(Regex("[0-9a-f]{32}"))) { "Invalid task ID" }
        require(archive.isNotEmpty() && archive.size <= MAX_ARCHIVE) { "Preview archive size is outside the allowed limit" }
        val actualHash = sha256(archive)
        require(actualHash.equals(expectedSha256, ignoreCase = true)) { "Preview archive SHA-256 mismatch" }
        val dest = File(root, "$taskId-${actualHash.take(12)}")
        if (dest.isDirectory) return validateExisting(taskId, dest, actualHash)
        val staging = File(root, ".$taskId-${actualHash.take(12)}-${System.nanoTime()}.partial")
        check(staging.mkdir()) { "Could not create private preview staging directory" }
        val seen = mutableSetOf<String>()
        var expanded = 0L
        try {
            ZipInputStream(ByteArrayInputStream(archive)).use { zip ->
                while (true) {
                    val entry = zip.nextEntry ?: break
                    val relative = safeRelativePath(entry.name)
                    if (relative.isEmpty()) { zip.closeEntry(); continue }
                    check(seen.add(relative)) { "Duplicate path in preview archive" }
                    val target = File(staging, relative)
                    check(target.canonicalPath.startsWith(staging.canonicalPath + File.separator)) { "Preview path escaped staging directory" }
                    if (entry.isDirectory) {
                        check(target.mkdirs() || target.isDirectory) { "Could not create preview folder" }
                    } else {
                        check(relative.substringAfterLast('.', "").lowercase() in ALLOWED_EXTENSIONS) { "Unsupported preview file type" }
                        target.parentFile?.let { check(it.mkdirs() || it.isDirectory) { "Could not create preview folder" } }
                        FileOutputStream(target).use { out ->
                            val buffer = ByteArray(16 * 1024)
                            var written = 0L
                            while (true) {
                                val count = zip.read(buffer)
                                if (count < 0) break
                                written += count; expanded += count
                                check(written <= MAX_FILE && expanded <= MAX_EXPANDED) { "Preview archive expands beyond allowed limits" }
                                out.write(buffer, 0, count)
                            }
                            out.fd.sync()
                        }
                    }
                    zip.closeEntry()
                    check(seen.size <= MAX_FILES) { "Too many preview files" }
                }
            }
            val index = staging.walkTopDown().firstOrNull { it.isFile && it.name.equals("index.html", true) }
                ?: error("Preview archive must contain index.html")
            check(dest.parentFile == root && staging.renameTo(dest)) { "Could not atomically publish verified preview" }
            val finalIndex = File(dest, index.relativeTo(staging).path)
            return Preview(taskId, dest, finalIndex, seen.toList().sorted(), actualHash)
        } catch (t: Throwable) {
            staging.deleteRecursively()
            throw t
        }
    }

    private fun validateExisting(taskId: String, directory: File, hash: String): Preview {
        val index = directory.walkTopDown().firstOrNull { it.isFile && it.name.equals("index.html", true) }
            ?: error("Existing preview is missing index.html")
        return Preview(taskId, directory, index, directory.walkTopDown().filter { it.isFile }.map { it.relativeTo(directory).path }.toList(), hash)
    }

    private fun safeRelativePath(name: String): String {
        require(name.isNotBlank() && !name.startsWith('/') && !name.startsWith('\\') && !name.contains('\\') && !name.contains('\u0000') && !Regex("^[A-Za-z]:").containsMatchIn(name)) { "Unsafe preview path" }
        val segments = name.split('/')
        require(segments.none { it == ".." || it == "." || it.isBlank() }) { "Unsafe preview path segment" }
        return segments.joinToString(File.separator)
    }

    companion object {
        private const val MAX_ARCHIVE = 20 * 1024 * 1024
        private const val MAX_EXPANDED = 40L * 1024 * 1024
        private const val MAX_FILE = 8L * 1024 * 1024
        private const val MAX_FILES = 200
        private val ALLOWED_EXTENSIONS = setOf("html", "htm", "css", "js", "json", "svg", "png", "jpg", "jpeg", "webp", "woff2", "txt", "md")
        fun sha256(data: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
    }
}
