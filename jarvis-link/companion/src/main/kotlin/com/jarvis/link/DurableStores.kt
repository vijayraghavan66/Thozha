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
class FileMessageOutbox(private val file: File) : MessageOutbox {
    private val items = linkedMapOf<String, String>()

    init {
        if (file.isFile) {
            val stored = LinkJson.parseObject(file.readText(UTF_8))
            @Suppress("UNCHECKED_CAST")
            (stored["items"] as? List<String>)?.forEach { raw -> items[LinkEnvelope.parse(raw).header.messageId] = raw }
        }
    }

    @Synchronized override fun enqueue(envelopeJson: String) {
        items[LinkEnvelope.parse(envelopeJson).header.messageId] = envelopeJson
        save()
    }

    @Synchronized override fun list(): List<String> = items.values.toList()

    @Synchronized override fun remove(messageId: String) {
        if (items.remove(messageId) != null) save()
    }

    private fun save() = writeAtomically(file, LinkJson.encode(mapOf("items" to items.values.toList())))
}

/**
 * Message IDs already processed, with expiry. Duplicate suppression must survive a restart, otherwise a
 * redelivered task could run twice after the companion process is killed between decrypt and ACK.
 */
class FileMessageInbox(private val file: File) : MessageInbox {
    private val expires = linkedMapOf<String, Long>()

    init {
        if (file.isFile) {
            LinkJson.parseObject(file.readText(UTF_8)).forEach { (id, value) -> expires[id] = value as Long }
        }
    }

    @Synchronized override fun recordIfNew(messageId: String, expiresAtUnixMs: Long): Boolean {
        if (expires.containsKey(messageId)) return false
        expires[messageId] = expiresAtUnixMs
        save()
        return true
    }

    @Synchronized override fun forgetExpired(nowUnixMs: Long) {
        if (expires.entries.removeIf { it.value <= nowUnixMs }) save()
    }

    private fun save() = writeAtomically(file, LinkJson.encode(expires.toMap()))
}
