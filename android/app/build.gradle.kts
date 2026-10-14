plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "org.ownpbx.myphone"
    compileSdk = 34

    defaultConfig {
        applicationId = "org.ownpbx.myphone"
        minSdk = 26
        targetSdk = 34
        versionCode = 13
        versionName = "0.6.0"
        val defaultServer = (project.findProperty("myphone.defaultServer") as String?).orEmpty()
            .replace("\\", "").replace("\"", "")
        buildConfigField("String", "DEFAULT_SERVER", "\"$defaultServer\"")
        // Phones only (keeps the APK smaller; Linphone ships native code per ABI).
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a") }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
    // F-Droid / reproducible builds: no dependency metadata blob.
    dependenciesInfo { includeInApk = false; includeInBundle = false }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.5")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("androidx.biometric:biometric:1.1.0")
    // biometric 1.1.0 pulls in fragment 1.2.5, whose FragmentActivity rejects the
    // Activity Result API's request codes ("Can only use lower 16 bits"), which
    // crashed Scan QR code with IllegalArgumentException. Use a current one.
    implementation("androidx.fragment:fragment-ktx:1.8.3")
    // Background checks for notifications (AndroidX, no Play Services).
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // Built-in phone: Linphone SDK, voice-only build (GPL-3.0; the app as a
    // whole is GPL-2.0-or-later, which allows combining with GPL-3.0).
    implementation("org.linphone.no-video:linphone-sdk-android:5.4.+")
    implementation("androidx.media:media:1.7.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // QR scanning for pairing: ZXing (Apache-2.0), no Google Play Services.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
