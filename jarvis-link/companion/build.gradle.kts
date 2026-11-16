plugins {
    kotlin("jvm") version "2.2.21"
}

kotlin { jvmToolchain(17) }

tasks.register<JavaExec>("verifyCompanionProtocol") {
    group = "verification"
    description = "Runs dependency-free Jarvis Link Companion crypto and delivery-state checks."
    dependsOn(tasks.testClasses)
    classpath = sourceSets["test"].runtimeClasspath
    mainClass.set("com.jarvis.link.CompanionProtocolTest")
    args(providers.gradleProperty("jarvis.fixture").orElse("../fixtures/envelope-v1.test.json").get())
}
