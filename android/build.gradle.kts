// Versions are pinned so the container build is reproducible. Bump them
// together: the Compose compiler plugin must match the Kotlin version.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
}
