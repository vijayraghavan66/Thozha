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

// The companion shares protocol sources from Jarvis-Lab. Resolve from either the canonical
// android-companion checkout or an isolated Git worktree nested under Jarvis-Lab/worktrees/.
val jarvisLabRoot = generateSequence(rootProject.projectDir) { it.parentFile }
    .take(5)
    .firstOrNull { it.resolve("jarvis-link/companion/src/main/kotlin").isDirectory }
    ?: error("Could not locate Jarvis-Lab/jarvis-link companion sources.")
val jarvisLinkRoot = jarvisLabRoot.resolve("jarvis-link")
val connectivityPocRoot = jarvisLabRoot.resolve("connectivity-poc")

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
    testImplementation("org.json:json:20250517")
    // Firebase is optional: its artifacts and FCM service are compiled only when google-services.json exists.
    // Firebase artifacts are not in the offline Gradle cache, so they must not be required for other builds.
    if (hasFirebaseConfig) {
        implementation(platform("com.google.firebase:firebase-bom:35.2.0"))
        implementation("com.google.firebase:firebase-messaging")
    }
    // QR decoding happens on-device with ZXing; first pairing does not depend on a Play services model download.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // Pinned to 0.16.1: official LiteRT-LM docs confirm this metadata is compatible with Kotlin 2.2.x.
    // We ship no model weights; users must explicitly import an eligible local model.
    implementation("com.google.ai.edge.litertlm:litertlm-android:0.16.1")
    // Sherpa-ONNX native Android runtime for genuinely local neural TTS (Piper/Kokoro model assets are user-managed).
    implementation(files("libs/sherpa-onnx-2.0.1.aar"))
    // Native Compose UI (Jarvis 3.0). BOM pins the Compose library versions together.
    implementation(platform("androidx.compose:compose-bom:2026.06.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.animation:animation")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.13.0")
}

tasks.register<JavaExec>("verifyLocalAi") {
    group = "verification"
    description = "Checks local model storage, explicit license gating, backend lifecycle and cancellation."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.localai.LocalAiRuntimeChecks")
}

tasks.register<JavaExec>("verifyVoicePolicy") {
    group = "verification"
    description = "Checks offline voice choice, language preference, audio-focus policy, and stale-session callback guards."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.voice.VoicePolicyChecks")
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

tasks.register<JavaExec>("verifyPairingInvitationProtocol") {
    group = "verification"
    description = "Verifies strict signed v1 USB and v2 local-Wi-Fi QR invitation parsing."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.data.PairingInvitationProtocolChecks")
}

tasks.register<JavaExec>("verifyLocalWifiPresentation") {
    group = "verification"
    description = "Checks that local identity pinning is never presented as Wi-Fi reachability and validates assistant-name input."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.presentation.LocalWifiPresentationChecks")
}

tasks.register<JavaExec>("verifyLocalGatewayRouting") {
    group = "verification"
    description = "Checks private DNS-SD address selection and exact pinned certificate DNS-name verification."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.data.LocalGatewayRoutingChecks")
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

tasks.register<JavaExec>("verifyFullAnswers") {
    group = "verification"
    description = "Runs full-answer regression checks: SHA-256 body verification, cut notices, and bubble text."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.link.FullAnswerChecksKt")
}

tasks.register<JavaExec>("verifyPresentation") {
    group = "verification"
    description = "Runs presentation checks for states, conversation bubbles, voice, and motion policy."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.presentation.PresentationChecksKt")
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
    args(connectivityPocRoot.resolve("telegram_adapter/tunnel-v1.fixture.json").absolutePath)
}

// ---- Jarvis Link transport (shared source with jarvis-link/companion; the Android app compiles it too) ----
// Protocol, HTTP relay client, durable stores and the CLI live in the Jarvis Link workspace so the relay,
// host and companion are tested from one copy of the code. This module builds them with its working toolchain.
android.sourceSets.getByName("main").java.srcDir(jarvisLinkRoot.resolve("companion/src/main/kotlin"))
android.sourceSets.getByName("test").java.srcDir(jarvisLinkRoot.resolve("companion/src/test/kotlin"))

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
    args(jarvisLinkRoot.resolve("fixtures/envelope-v1.test.json").absolutePath)
}


tasks.register<JavaExec>("verifyExecutionLanes") {
    group = "verification"
    description = "Checks sends and local refresh can proceed while sync is blocked."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(layout.buildDirectory.dir("tmp/kotlin-classes/debug"), layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest")) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.ui.ExecutionLanesCheck")
}

tasks.register<JavaExec>("verifyFastCommand") {
    group = "verification"
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"), layout.buildDirectory.dir("tmp/kotlin-classes/debug"), configurations.getByName("debugRuntimeClasspath"))
    mainClass.set("com.jarvis.companion.fastpath.FastCommandCheckKt")
}

tasks.register<JavaExec>("verifyDevelopmentScope") {
    group = "verification"
    description = "Checks that only explicit software-building requests select the supervised development scope."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.fastpath.DevelopmentScopeCheckKt")
}

tasks.register<JavaExec>("verifyOpenJarvisApprovalReview") {
    group = "verification"
    description = "Checks complete, digest-bound OpenJarvis development-plan approvals are parsed fail-closed."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.data.ApprovalReviewParsingChecksKt")
}

tasks.register<JavaExec>("verifyTaskResultSummary") {
    group = "verification"
    description = "Prevents nullable task fields from rendering as the literal string null in Home."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(
        layout.buildDirectory.dir("tmp/kotlin-classes/debug"),
        layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest"),
    ) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.data.TaskSummaryChecksKt")
}

tasks.register<JavaExec>("verifyLocalWeather") {
    group = "verification"
    description = "Runs Open-Meteo fixture, local-routing, malformed-data and cancellation checks."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(layout.buildDirectory.dir("tmp/kotlin-classes/debug"), layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest")) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.voice.LocalWeatherChecksKt")
}

tasks.register<JavaExec>("verifyLocalTasks") {
    group = "verification"
    description = "Checks deterministic phone calculation and conversion task routing/execution."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(layout.buildDirectory.dir("tmp/kotlin-classes/debug"), layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest")) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.localtask.LocalTaskChecksKt")
}

tasks.register<JavaExec>("verifyTaskNotifications") {
    group = "verification"
    description = "Checks task milestone notification eligibility and stable event deduplication keys."
    dependsOn("compileDebugUnitTestKotlin")
    classpath = files(layout.buildDirectory.dir("tmp/kotlin-classes/debug"), layout.buildDirectory.dir("tmp/kotlin-classes/debugUnitTest")) + configurations.getByName("debugUnitTestRuntimeClasspath")
    mainClass.set("com.jarvis.companion.link.TaskNotificationChecksKt")
}
