package com.jarvis.companion.fastpath

fun main() {
    check(FastCommandClassifier.classify("  Open   Calculator ") == FastCommand.OpenApplication(AllowedMacApplication.CALCULATOR))
    check(FastCommandClassifier.classify("is my Mac online") == FastCommand.MacStatus)
    check(FastCommandClassifier.classify("status for task 0123456789abcdef0123456789abcdef") == FastCommand.TaskStatus("0123456789abcdef0123456789abcdef"))
    check(FastCommandClassifier.classify("open Terminal") == null)
    check(FastCommandClassifier.classify("open calculator and delete files") == null)
    check(FastCommandClassifier.classify("what is the status for task 0123456789abcdef0123456789abcdef please") == null)
    check(FastCommandClassifier.classify("open calculator; rm -rf /") == null)
    check(FastCommandClassifier.classify("open calculator" + "!".repeat(300)) == null)
    println("Fast command classifier checks passed")
}
