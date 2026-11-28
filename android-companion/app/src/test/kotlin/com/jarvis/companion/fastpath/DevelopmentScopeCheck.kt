package com.jarvis.companion.fastpath

fun main() {
    var assertions = 0
    fun expect(condition: Boolean) {
        assertions++
        check(condition) { "Development scope classifier check $assertions failed" }
    }

    val explicit = DevelopmentIntentClassifier.parseTaskCommand("  /task Build a command-line task manager  ")
    expect(explicit.isCommand && explicit.objective == "Build a command-line task manager")
    expect(DevelopmentIntentClassifier.capabilityScope("/task Build a command-line task manager") == "development")
    expect(DevelopmentIntentClassifier.capabilityScope("/TASK Implement a small website") == "development")
    expect(DevelopmentIntentClassifier.parseTaskCommand("/tasking write a script").isCommand.not())
    val missing = DevelopmentIntentClassifier.parseTaskCommand("/task   ")
    expect(missing.isCommand && missing.objective == null)
    expect(DevelopmentIntentClassifier.capabilityScope("/task") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Create a Python expense tracker with tests") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Build a command-line task manager") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Implement a small website") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Write a script that sorts local photos") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("What's my Mac's battery level?") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Open Calculator on my Mac") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Create a meeting agenda") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Build me a wooden bookshelf") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("Don't create a software project") == "assistant")
    expect(DevelopmentIntentClassifier.capabilityScope("") == "assistant")

    println("Explicit task command checks passed: $assertions assertions")
}
