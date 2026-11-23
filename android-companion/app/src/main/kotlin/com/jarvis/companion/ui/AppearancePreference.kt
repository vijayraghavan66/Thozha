package com.jarvis.companion.ui

import android.content.Context

/** The visual appearance preference is presentation-only and never changes task or transport state. */
object AppearancePreference {
    private const val PREFS = "thozha_appearance"
    private const val LIGHT = "light"

    fun isLight(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(LIGHT, false)

    fun setLight(context: Context, enabled: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(LIGHT, enabled).apply()
    }
}
