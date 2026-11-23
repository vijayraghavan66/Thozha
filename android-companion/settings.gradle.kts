// Local mirror first: built by jarvis-link/scripts/build-local-maven-mirror.sh from Gradle's own cache.
// Maven Central is unreachable on the build network (404 for every artifact), so the mirror supplies
// the Kotlin/AGP artifacts that earlier builds already downloaded. Absent mirror = normal repositories.
pluginManagement {
    repositories {
        val localMirror = File(System.getProperty("user.home"), ".jarvis-link-maven-mirror")
        if (localMirror.isDirectory) maven { url = uri(localMirror) }
        // Google's operated Maven Central mirror (same artifacts, served from storage.googleapis.com) comes
        // before the unreachable central host so that module metadata and jars come from the same repository.
        google(); maven { url = uri("https://maven-central.storage.googleapis.com/maven2") }; mavenCentral(); gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        val localMirror = File(System.getProperty("user.home"), ".jarvis-link-maven-mirror")
        if (localMirror.isDirectory) maven { url = uri(localMirror) }
        google(); maven { url = uri("https://maven-central.storage.googleapis.com/maven2") }; mavenCentral()
    }
}
rootProject.name = "JarvisCompanion"
include(":app")
