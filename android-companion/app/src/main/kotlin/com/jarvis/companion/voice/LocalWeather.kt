package com.jarvis.companion.voice

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.concurrent.atomic.AtomicLong

/** Credential-free Open-Meteo client. Calls are synchronous by design; invoke from an IO dispatcher. */
data class WeatherSnapshot(val city: String, val temperatureC: Double, val precipitationProbability: Int, val fetchedAt: Instant) {
    fun response(): String = buildString {
        append("In $city, it is ${temperatureC.toInt()}°C. ")
        append(if (precipitationProbability >= 40) "Rain is possible ($precipitationProbability% chance), so carrying an umbrella is a good idea." else "There is no significant rain expected ($precipitationProbability% chance), so you probably do not need an umbrella.")
    }
    fun isStale(now: Instant = Instant.now()): Boolean = fetchedAt.isBefore(now.minus(2, ChronoUnit.HOURS))
}

fun interface WeatherHttp { fun get(address: String): String }

class LocalWeatherClient(
    private val now: () -> Instant = Instant::now,
    private val http: WeatherHttp = WeatherHttp(::httpGet),
) {
    fun current(city: String): WeatherSnapshot {
        require(city.isNotBlank()) { "Choose a city to check the weather." }
        val query = URLEncoder.encode(city.trim(), Charsets.UTF_8.name())
        val geo = JSONObject(http.get("https://geocoding-api.open-meteo.com/v1/search?name=$query&count=1&language=en&format=json"))
        val place = geo.getJSONArray("results").optJSONObject(0) ?: error("City not found. Choose a nearby city and retry.")
        val lat = place.getDouble("latitude"); val lon = place.getDouble("longitude")
        val forecast = JSONObject(http.get("https://api.open-meteo.com/v1/forecast?latitude=$lat&longitude=$lon&current=temperature_2m&daily=precipitation_probability_max&forecast_days=1&timezone=auto"))
        val current = forecast.getJSONObject("current")
        val rain = forecast.getJSONObject("daily").getJSONArray("precipitation_probability_max").getInt(0)
        return WeatherSnapshot(place.optString("name", city), current.getDouble("temperature_2m"), rain, now())
    }
    companion object {
    private fun httpGet(address: String): String {
        val connection = URL(address).openConnection() as HttpURLConnection
        connection.connectTimeout = 5000; connection.readTimeout = 5000; connection.requestMethod = "GET"
        return try { if (connection.responseCode !in 200..299) error("Weather service unavailable (${connection.responseCode}). Retry when online."); connection.inputStream.bufferedReader().use { it.readText() } } finally { connection.disconnect() }
    }
    }
}

/** Invalidates results from cancelled turns or a disposed LiveScreen. */
class WeatherRequestEpoch {
    private val epoch = AtomicLong(0)
    fun begin(): Long = epoch.incrementAndGet()
    fun cancel() { epoch.incrementAndGet() }
    fun isCurrent(token: Long): Boolean = epoch.get() == token
}

object WeatherIntentRouter {
    fun isWeatherRequest(text: String): Boolean = listOf("weather", "umbrella", "rain", "how's the day", "hows the day").any { text.lowercase().contains(it) }
    fun route(text: String, localWeather: () -> Unit, mac: () -> Unit) {
        if (isWeatherRequest(text)) localWeather() else mac()
    }
}
