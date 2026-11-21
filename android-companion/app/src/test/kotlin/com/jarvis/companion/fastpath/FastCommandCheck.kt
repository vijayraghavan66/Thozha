package com.jarvis.companion.fastpath

fun main() {
    var assertions = 0
    fun expect(condition: Boolean) {
        assertions++
        check(condition) { "Fast command classifier check $assertions failed" }
    }
    expect(FastCommandClassifier.classify("  Open   Calculator ") == FastCommand.OpenApplication(AllowedMacApplication.CALCULATOR))
    expect(AllowedMacApplication.CALCULATOR.bundleId == "com.apple.calculator")
    expect(FastCommandClassifier.classify("Open Calculator on my Mac.") == FastCommand.OpenApplication(AllowedMacApplication.CALCULATOR))
    expect(FastCommandClassifier.classify("Open Spotify on my Mac") == FastCommand.OpenApplication(AllowedMacApplication.SPOTIFY))
    expect(FastCommandClassifier.classify("launch Spotify") == FastCommand.OpenApplication(AllowedMacApplication.SPOTIFY))
    expect(AllowedMacApplication.SPOTIFY.bundleId == "com.spotify.client")
    expect(FastCommandClassifier.classify("is my Mac online") == FastCommand.MacStatus)
    expect(FastCommandClassifier.classify("What's my Mac's battery level?") == FastCommand.MacBatteryStatus)
    expect(FastCommandClassifier.classify("what is my mac battery level") == FastCommand.MacBatteryStatus)
    expect(FastCommandClassifier.classify("Jarvis tell me the Mac battery status and system uptime") == FastCommand.MacSystemInfo)
    expect(FastCommandClassifier.classify("tell me the Mac battery status and system uptime, then open Calculator") == null)
    expect(FastCommandClassifier.classify("status for task 0123456789abcdef0123456789abcdef") == FastCommand.TaskStatus("0123456789abcdef0123456789abcdef"))
    expect(FastCommandClassifier.classify("open Terminal") == null)
    expect(FastCommandClassifier.classify("what is my laptop battery level") == null)
    expect(FastCommandClassifier.classify("open Spotify and delete cache") == null)
    expect(FastCommandClassifier.classify("open Spotify with argument --url=https://evil.example") == null)
    expect(FastCommandClassifier.classify("open calculator and delete files") == null)
    expect(FastCommandClassifier.classify("what is the status for task 0123456789abcdef0123456789abcdef please") == null)
    expect(FastCommandClassifier.classify("open calculator; rm -rf /") == null)
    expect(FastCommandClassifier.classify("open calculator" + "!".repeat(300)) == null)
    println("Fast command classifier checks passed: $assertions assertions")
}
