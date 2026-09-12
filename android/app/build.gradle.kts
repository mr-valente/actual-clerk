plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// The keystore lives outside the source tree (android/keystore/, ignored by
// git). scripts/build-android.sh creates one the first time and reuses it
// after that, because Android only installs an update signed with the same
// key as the app it replaces.
val keystorePath = System.getenv("CLERK_KEYSTORE") ?: "${rootDir}/keystore/clerk.jks"
val keystorePassword = System.getenv("CLERK_KEYSTORE_PASSWORD") ?: "actual-clerk"

android {
    namespace = "dev.actualclerk.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.actualclerk.companion"
        minSdk = 26
        targetSdk = 35
        versionCode = (System.getenv("CLERK_APP_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("CLERK_APP_VERSION") ?: "0.1.0"
    }

    signingConfigs {
        create("release") {
            storeFile = file(keystorePath)
            storePassword = keystorePassword
            keyAlias = "clerk"
            keyPassword = keystorePassword
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources.excludes += setOf("META-INF/AL2.0", "META-INF/LGPL2.1", "META-INF/*.version")
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.work:work-runtime:2.10.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
