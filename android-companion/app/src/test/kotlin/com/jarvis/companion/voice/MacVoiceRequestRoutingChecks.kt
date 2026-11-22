package com.jarvis.companion.voice

object MacVoiceRequestRoutingChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        check(MacVoiceRequestRouting.shouldSubmitThroughSupervisor("Open Calculator on my Mac"))
        check(MacVoiceRequestRouting.shouldSubmitThroughSupervisor("Create a Python task manager with tests"))
        check(MacVoiceRequestRouting.shouldSubmitThroughSupervisor("Build a static website"))
        check(MacVoiceRequestRouting.shouldSubmitThroughSupervisor("Create a landing page and show me"))
        check(!MacVoiceRequestRouting.shouldSubmitThroughSupervisor("How are you today?"))
        check(!MacVoiceRequestRouting.shouldSubmitThroughSupervisor("What's the weather in Chennai?"))
        check(!MacVoiceRequestRouting.shouldSubmitThroughSupervisor("Tell me how to open a project"))
        println("Mac voice request routing checks passed: 7 assertions")
    }
}
