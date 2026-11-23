package com.jarvis.companion.fastpath

fun main() {
    var assertions = 0
    fun expect(condition: Boolean) {
        assertions++
        check(condition) { "Development scope classifier check $assertions failed" }
    }

    expect(DevelopmentIntentClassifier.capabilityScope(
        "Create a Python expense tracker with tests and persistent storage",
    ) == "development")
    expect(DevelopmentIntentClassifier.capabilityScope("Build a command-line task manager") == "development")
    expect(DevelopmentIntentClassifier.capabilityScope("Implement a small website") == "development")
    expect(DevelopmentIntentClassifier.capabilityScope("Write a script that sorts local photos") == "development")
    expect(DevelopmentIntentClassifier.capabilityScope("What's my Mac's battery level?") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Open Calculator on my Mac") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Create a meeting agenda") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Build me a wooden bookshelf") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Don't create a software project") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("") == "assistant")

    println("Development scope classifier checks passed: $assertions assertions")
}
