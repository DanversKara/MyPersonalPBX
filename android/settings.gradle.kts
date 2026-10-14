pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google(); mavenCentral()
        // Linphone SDK (GPL-3.0): the built-in phone engine.
        maven("https://download.linphone.org/maven_repository") {
            content { includeGroupByRegex("org\\.linphone.*") }
        }
    }
}
rootProject.name = "MyPhone"
include(":app")
