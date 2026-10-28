plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

// The one place the version lives. .github/workflows/android-auto-release.yml
// edits this exact line, so keep it on a single line in this form.
val APP_VERSION_NAME = "0.8.9"

// versionCode is derived, never hand-maintained: 0.7.3 -> 703, 1.2.3 -> 10203.
// Rebuilding any tag anywhere gives the same number.
fun versionCodeOf(name: String): Int {
    val (major, minor, patch) = name.substringBefore('-').split('.').map { it.toInt() }
    return major * 10000 + minor * 100 + patch
}

android {
    namespace = "org.ownpbx"
    compileSdk = 36

    defaultConfig {
        applicationId = "org.ownpbx"
        minSdk = 26
        targetSdk = 36
        versionCode = versionCodeOf(APP_VERSION_NAME)
        versionName = APP_VERSION_NAME
        val defaultServer = (project.findProperty("myphone.defaultServer") as String?).orEmpty()
            .replace("\\", "").replace("\"", "")
        buildConfigField("String", "DEFAULT_SERVER", "\"$defaultServer\"")
        // Phones only (keeps the APK smaller; Linphone ships native code per ABI).
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a") }
    }

    // Release signing comes from the environment (GitHub secrets in CI). With
    // nothing set the release build is simply unsigned, as it was before.
    signingConfigs {
        create("release") {
            val ks = System.getenv("MYPHONE_KEYSTORE_FILE")
            if (!ks.isNullOrBlank()) {
                storeFile = file(ks)
                storePassword = System.getenv("MYPHONE_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("MYPHONE_KEY_ALIAS")
                keyPassword = System.getenv("MYPHONE_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release").takeIf { it.storeFile != null }
            // R8 broke Linphone audio in 0.7.3 (no audio either way) and the
            // keep rules didn't fully fix it. Ship unshrunk until the exact
            // R8 issue is identified; a larger APK beats a silent phone.
            isMinifyEnabled = false
            isShrinkResources = false
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
    implementation("androidx.browser:browser:1.8.0")
    // Built-in phone: Linphone SDK, voice-only build (GPL-3.0; the app as a
    // whole is GPL-2.0-or-later, which allows combining with GPL-3.0).
    implementation("org.linphone.no-video:linphone-sdk-android:5.4.128")
    implementation("androidx.media:media:1.7.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // QR scanning for pairing: ZXing (Apache-2.0), no Google Play Services.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    debugImplementation("androidx.compose.ui:ui-tooling")
}

// Used by the release workflow to check the tag against the build.
tasks.register("printVersionName") { doLast { println(APP_VERSION_NAME) } }
