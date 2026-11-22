package com.jarvis.companion.voice

import android.content.Context
import android.os.StatFs
import android.util.Log
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream

object KokoroAssets {
    private const val TAG = "JarvisNeuralTts"
    const val MODEL_BYTES = 345_555_491L
    fun provision(context: Context): File {
        val ext = context.getExternalFilesDir(null) ?: error("getExternalFilesDir returned null")
        val src = File(ext, "kokoro-en-v0_19")
        val dest = File(context.filesDir, "models/kokoro-en-v0_19")
        val modelSrc = File(src, "model.onnx")
        val modelDst = File(dest, "model.onnx")
        val stat = StatFs(context.filesDir.absolutePath)
        Log.i(TAG, "event=provision_source dir=${src.absolutePath} exists=${src.isDirectory} model=${modelSrc.absolutePath} exists=${modelSrc.exists()} readable=${modelSrc.canRead()} bytes=${modelSrc.length()} internal_free=${stat.availableBytes}")
        check(modelSrc.isFile && modelSrc.canRead() && modelSrc.length() == MODEL_BYTES) { "Source model unavailable or wrong size: ${modelSrc.absolutePath}, exists=${modelSrc.exists()}, readable=${modelSrc.canRead()}, bytes=${modelSrc.length()}" }
        check(stat.availableBytes > MODEL_BYTES + 64L * 1024 * 1024) { "Insufficient internal storage for atomic model copy: free=${stat.availableBytes}" }
        val files = listOf("model.onnx", "voices.bin", "tokens.txt", "lexicon-en-us.txt")
        files.forEach { name -> copyAtomic(File(src, name), File(dest, name)) }
        copyTree(File(src, "espeak-ng-data"), File(dest, "espeak-ng-data"))
        files.forEach { name ->
            val f = File(dest, name)
            check(f.isFile && f.canRead() && f.length() == File(src, name).length()) { "Provisioned asset invalid: ${f.absolutePath}, exists=${f.exists()}, readable=${f.canRead()}, bytes=${f.length()}" }
        }
        check(File(dest, "espeak-ng-data/phondata-manifest").canRead()) { "eSpeak phondata manifest missing under ${dest.absolutePath}" }
        Log.i(TAG, "event=provision_complete dir=${dest.absolutePath} model_bytes=${modelDst.length()} model_readable=${modelDst.canRead()} voices_bytes=${File(dest,"voices.bin").length()} tokens_bytes=${File(dest,"tokens.txt").length()}")
        return dest
    }
    private fun copyTree(src: File, dst: File) {
        check(src.isDirectory) { "Required eSpeak data directory missing: ${src.absolutePath}" }
        src.walkTopDown().filter { it.isDirectory }.forEach { File(dst, it.relativeTo(src).path).mkdirs() }
        src.walkTopDown().filter { it.isFile }.forEach { copyAtomic(it, File(dst, it.relativeTo(src).path)) }
    }
    private fun copyAtomic(src: File, dst: File) {
        check(src.isFile && src.canRead()) { "Source asset unreadable: ${src.absolutePath}" }
        dst.parentFile?.mkdirs()
        if (dst.isFile && dst.canRead() && dst.length() == src.length()) return
        val tmp = File(dst.parentFile, dst.name + ".partial")
        try {
            FileInputStream(src).use { input -> FileOutputStream(tmp).use { output -> input.copyTo(output, 1024 * 1024); output.fd.sync() } }
            check(tmp.length() == src.length()) { "Short copy ${src.absolutePath}: ${tmp.length()} != ${src.length()}" }
            if (dst.exists() && !dst.delete()) error("Could not replace stale asset ${dst.absolutePath}")
            check(tmp.renameTo(dst)) { "Atomic rename failed ${tmp.absolutePath} -> ${dst.absolutePath}" }
        } catch (e: Exception) {
            tmp.delete()
            Log.e(TAG, "event=copy_failed src=${src.absolutePath} dst=${dst.absolutePath} src_bytes=${src.length()} tmp_bytes=${tmp.length()} type=${e.javaClass.simpleName} detail=${e.message}")
            throw e
        }
    }
}
