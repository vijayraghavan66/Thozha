// Maven Central (repo.maven.apache.org) returns 404 for every artifact from the build network
// (verified with junit:junit:4.13.2). gradlePluginPortal() serves the same Central artifacts
// (https://plugins.gradle.org/m2) and google() serves AGP/AndroidX, so Central is not listed.
pluginManagement { repositories { google(); gradlePluginPortal() } }
dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); gradlePluginPortal() } }
rootProject.name = "jarvis-link-companion"
