package com.jarvis.companion.telegram

import android.content.Context

/**
 * Telegram API credentials entered on the phone. Stored only in this app's private preferences; never read
 * from the Mac Keychain, never embedded in the APK, never displayed after saving.
 */
class TelegramCredentialStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun load(): ApiCredentials? {
        val id = prefs.getString(KEY_ID, null) ?: return null
        val hash = prefs.getString(KEY_HASH, null) ?: return null
        return ApiCredentials.parse(id, hash)
    }

    /** Returns false when the input is invalid; nothing is stored in that case. */
    fun save(rawId: String, rawHash: String): Boolean {
        val creds = ApiCredentials.parse(rawId, rawHash) ?: return false
        prefs.edit()
            .putString(KEY_ID, creds.apiId.toString())
            .putString(KEY_HASH, creds.apiHash)
            .commit()
        return true
    }

    /** Per-install TDLib database encryption key, created on first use and kept only in private preferences. */
    fun databaseKey(): ByteArray {
        prefs.getString(KEY_DB, null)?.let { return android.util.Base64.decode(it, android.util.Base64.NO_WRAP) }
        val fresh = ByteArray(32).also { java.security.SecureRandom().nextBytes(it) }
        prefs.edit().putString(KEY_DB, android.util.Base64.encodeToString(fresh, android.util.Base64.NO_WRAP)).commit()
        return fresh
    }

    fun clear() {
        prefs.edit().remove(KEY_ID).remove(KEY_HASH).remove(KEY_DB).commit()
    }

    private companion object {
        const val PREFS = "jarvis_telegram_credentials"
        const val KEY_ID = "api_id"
        const val KEY_HASH = "api_hash"
        const val KEY_DB = "tdlib_db_key"
    }
}
