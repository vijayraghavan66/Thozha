package com.jarvis.link

import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.charset.StandardCharsets.UTF_8

/** Writes via a temporary file and atomic rename so a crash never leaves a half-written store. */
internal fun writeAtomically(target: File, text: String) {
    target.parentFile?.mkdirs()
    val temp = File(target.parentFile, target.name + ".tmp")
    temp.writeText(text, UTF_8)
    Files.move(temp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
}

/** Encrypted envelopes waiting for relay delivery. Survives restarts; replays are idempotent at the relay. */
private object FileStoreLock

class FileMessageOutbox(private val file: File) : MessageOutbox {
    private fun readItems(): LinkedHashMap<String, String> {
        val items = linkedMapOf<String, String>()
        if (file.isFile) {
            @Suppress("UNCHECKED_CAST")
            (LinkJson.parseObject(file.readText(UTF_8))["items"] as? List<String>)?.forEach { raw ->
                items[LinkEnvelope.parse(raw).header.messageId] = raw
            }
        }
        return items
    }
    override fun enqueue(envelopeJson: String) = synchronized(FileStoreLock) {
        val items = readItems()
        items[LinkEnvelope.parse(envelopeJson).header.messageId] = envelopeJson
        writeAtomically(file, LinkJson.encode(mapOf("items" to items.values.toList())))
    }
    override fun list(): List<String> = synchronized(FileStoreLock) { readItems().values.toList() }
    override fun remove(messageId: String) = synchronized(FileStoreLock) {
        val items = readItems()
        if (items.remove(messageId) != null) writeAtomically(file, LinkJson.encode(mapOf("items" to items.values.toList())))
        Unit
    }
}

/** Durable replay IDs shared by all runtime instances in the process. */
class FileMessageInbox(private val file: File) : MessageInbox {
    private fun readExpiry(): LinkedHashMap<String, Long> {
        val entries = linkedMapOf<String, Long>()
        if (file.isFile) LinkJson.parseObject(file.readText(UTF_8)).forEach { (id, value) -> entries[id] = value as Long }
        return entries
    }
    override fun recordIfNew(messageId: String, expiresAtUnixMs: Long): Boolean = synchronized(FileStoreLock) {
        val entries = readExpiry()
        if (entries.containsKey(messageId)) return@synchronized false
        entries[messageId] = expiresAtUnixMs
        writeAtomically(file, LinkJson.encode(entries))
        true
    }
    override fun forgetExpired(nowUnixMs: Long) = synchronized(FileStoreLock) {
        val entries = readExpiry()
        if (entries.entries.removeIf { it.value <= nowUnixMs }) writeAtomically(file, LinkJson.encode(entries))
        Unit
    }
}
