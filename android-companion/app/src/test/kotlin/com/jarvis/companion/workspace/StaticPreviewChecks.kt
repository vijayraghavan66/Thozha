package com.jarvis.companion.workspace

import java.io.ByteArrayOutputStream
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

private fun archive(vararg entries: Pair<String,String>): ByteArray {
    val bytes = ByteArrayOutputStream()
    ZipOutputStream(bytes).use { zip -> entries.forEach { (name, text) -> zip.putNextEntry(ZipEntry(name)); zip.write(text.toByteArray()); zip.closeEntry() } }
    return bytes.toByteArray()
}
fun main() {
    val root = createTempDir(prefix = "thozha-preview-test-")
    try {
        val store = StaticPreviewStore(root, Unit)
        val taskId = "a".repeat(32)
        val valid = archive("site/index.html" to "<h1>Generated</h1>", "site/app.css" to "h1{color:green}")
        val digest = StaticPreviewStore.sha256(valid)
        val installed = store.install(taskId, valid, digest)
        check(installed.indexHtml.readText() == "<h1>Generated</h1>")
        check(installed.files.size == 2 && installed.archiveSha256 == digest)
        check(runCatching { store.install("../".padEnd(32, 'a'), valid, digest) }.isFailure)
        check(runCatching { store.install(taskId, valid, "0".repeat(64)) }.isFailure)
        val escape = archive("../outside.html" to "bad", "index.html" to "bad")
        check(runCatching { store.install("b".repeat(32), escape, StaticPreviewStore.sha256(escape)) }.isFailure)
        val noIndex = archive("readme.md" to "no preview")
        check(runCatching { store.install("c".repeat(32), noIndex, StaticPreviewStore.sha256(noIndex)) }.isFailure)
        println("Static preview security checks passed: 6 assertions")
    } finally { root.deleteRecursively() }
}
