package com.jarvis.companion.voice

import java.time.Instant

private fun checkWeather(value: Boolean, message: String) { check(value) { message } }
fun main() {
    val geo = """{"results":[{"name":"Chennai","latitude":13.08,"longitude":80.27}]}"""
    val rain = """{"current":{"temperature_2m":31.4},"daily":{"precipitation_probability_max":[80]}}"""
    val clear = """{"current":{"temperature_2m":22.0},"daily":{"precipitation_probability_max":[5]}}"""
    var hits = 0
    val client = LocalWeatherClient({ Instant.parse("2026-11-22T10:00:00Z") }, WeatherHttp { url ->
        hits++
        when { url.contains("geocoding") -> geo; else -> rain }
    })
    val wet = client.current("Chennai")
    checkWeather(wet.precipitationProbability == 80 && wet.response().contains("umbrella"), "rain recommendation")
    val dry = LocalWeatherClient(http = WeatherHttp { if (it.contains("geocoding")) geo else clear }).current("Chennai")
    checkWeather(dry.precipitationProbability == 5 && dry.response().contains("probably do not need"), "clear recommendation")
    checkWeather(hits == 2, "geocoding and forecast requested")
    checkWeather(runCatching { client.current(" ") }.isFailure, "missing city fails closed")
    checkWeather(runCatching { LocalWeatherClient(http = WeatherHttp { error("offline") }).current("Chennai") }.isFailure, "network error surfaced")
    checkWeather(runCatching { LocalWeatherClient(http = WeatherHttp { if (it.contains("geocoding")) geo else "{}" }).current("Chennai") }.isFailure, "malformed forecast rejected")
    var local = 0; var mac = 0
    WeatherIntentRouter.route("Should I carry an umbrella?", { local++ }, { mac++ })
    checkWeather(local == 1 && mac == 0, "weather must stay local")
    checkWeather(wet.isStale(Instant.parse("2026-11-22T13:00:00Z")), "stale data recognized")
    println("Local weather fixture/routing checks passed: 8 assertions")
}
