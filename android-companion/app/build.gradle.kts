plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.gms.google-services") apply false
}

val hasFirebaseConfig = file("google-services.json").isFile
if (hasFirebaseConfig) {
    pluginManager.apply("com.google.gms.google-services")
}

android {
    namespace = "com.jarvis.companion"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.jarvis.companion"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "0.1.0"
        buildConfigField("boolean", "FCM_CONFIGURED", hasFirebaseConfig.toString())
        manifestPlaceholders["fcmMessagingServiceEnabled"] = hasFirebaseConfig.toString()
        // Vivo V2514 is arm64-v8a. TDLib is built for this ABI only (official source build, see PROGRESS.md).
        ndk { abiFilters += listOf("arm64-v8a") }
    }
    sourceSets.getByName("main") {
        java.srcDir("src/main/tdlib-java")  // official TDLib Java bindings (org.drinkless.tdlib), copied from td/example/android
    }

    buildTypes {
        debug { applicationIdSuffix = ".debug" }
        release { isMinifyEnabled = false }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    buildFeatures {
        buildConfig = true
        compose = true
    }
}

dependencies {
    // Firebase is optional: its artifacts and FCM service are compiled only when google-services.json exists.
    // Firebase artifacts are not in the offline Gradle cache, so they must not be required for other builds.
    if (hasFirebaseConfig) {
        implementation(platform("com.google.firebase:firebase-bom:35.2.0"))
        implementation("com.google.firebase:firebase-messaging")
    }
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
    // Native Compose UI (Jarvis 3.0). BOM pins the Compose library versions together.
    implementation(platform("androidx.compose:compose-bom:2026.06.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.animation:animation")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.13.0")
}

if (!hasFirebaseConfig) {
    tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile>().configureEach {
        exclude("**/notifications/**")
    }
}

tasks.register<JavaExec>("verifyMigrationIdentity") {
    group = "verification"
    description = "Runs the deterministic JVM-only trust and Tailscale boundary vectors."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.data.ServerMigrationIdentityTest")
}

tasks.register<JavaExec>("verifyAssistantFormatting") {
    group = "verification"
    description = "Runs dependency-free assistant-text formatting and escaping regression vectors."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.AssistantTextFormatterTest")
}

tasks.register<JavaExec>("verifyDebugTaskPayload") {
    group = "verification"
    description = "Verifies exact UTF-8 preservation for the debug-only Base64URL ADB task input."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.link.DebugTaskPayloadTest")
}

tasks.register<JavaExec>("verifyTelegramModel") {
    group = "verification"
    description = "Runs the JVM-only Telegram connection, authorization, ping and credential checks (no TDLib, no network)."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.telegram.TelegramModelChecksKt")
}

tasks.register<JavaExec>("verifyTelegramTunnel") {
    group = "verification"
    description = "Checks the Jarvis Link-over-Telegram tunnel: framing, cross-language parity with the Mac encoder, exchange and timeout."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.telegram.TelegramTunnelChecksKt")
    args(rootProject.file("../connectivity-poc/telegram_adapter/tunnel-v1.fixture.json").absolutePath)
}

// ---- Jarvis Link transport (shared source with jarvis-link/companion; the Android app compiles it too) ----
// Protocol, HTTP relay client, durable stores and the CLI live in the Jarvis Link workspace so the relay,
// host and companion are tested from one copy of the code. This module builds them with its working toolchain.
android.sourceSets.getByName("main").java.srcDir("../../jarvis-link/companion/src/main/kotlin")
android.sourceSets.getByName("test").java.srcDir("../../jarvis-link/companion/src/test/kotlin")

val linkCompanionCliJar by tasks.registering(Jar::class) {
    group = "verification"
    description = "Runnable JVM CLI for the Jarvis Link Kotlin transport (driven by the host integration tests)."
    dependsOn("compileDebugKotlin")
    archiveFileName.set("jarvis-link-cli.jar")
    destinationDirectory.set(layout.buildDirectory.dir("libs"))
    manifest { attributes("Main-Class" to "com.jarvis.link.CompanionCli") }
    from(layout.buildDirectory.dir("tmp/kotlin-classes/debug"))
    from({
        configurations.getByName("debugRuntimeClasspath")
            .filter { it.name.startsWith("kotlin-stdlib") }
            .map { zipTree(it) }
    })
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
    exclude("META-INF/*.SF", "META-INF/*.RSA", "META-INF/*.DSA", "META-INF/versions/**/module-info.class")
}

tasks.register<JavaExec>("verifyLinkProtocol") {
    group = "verification"
    description = "Runs the Jarvis Link protocol checks (crypto, fixture interop, offline delivery) on the JVM."
    dependsOn("compileDebugUnitTestKotlin", "linkCompanionCliJar")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugRuntimeClasspath")
    mainClass.set("com.jarvis.link.CompanionProtocolTest")
    args(rootProject.file("../jarvis-link/fixtures/envelope-v1.test.json").absolutePath)
}

