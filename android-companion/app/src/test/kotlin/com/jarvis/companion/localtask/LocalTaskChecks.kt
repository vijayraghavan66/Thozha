package com.jarvis.companion.localtask

fun main() {
    val calc = LocalTaskExecutor.classify("Jarvis, calculate 19 * 7") ?: error("calc not routed")
    check(LocalTaskExecutor.execute(calc).response == "19 * 7 = 133")
    val conversion = LocalTaskExecutor.classify("convert 10 km to miles") ?: error("conversion not routed")
    check(LocalTaskExecutor.execute(conversion).response == "10 km = 6.2137 miles")
    check(LocalTaskExecutor.classify("convert 5 kg to miles") == null)
    check(LocalTaskExecutor.classify("run this shell command: echo hi") == null)
    val zero = LocalTaskExecutor.classify("what is 4 / 0") ?: error("division route")
    check(runCatching { LocalTaskExecutor.execute(zero) }.exceptionOrNull()?.message == "Cannot divide by zero.")
    println("Phone local deterministic task checks passed: 5 assertions")
}
