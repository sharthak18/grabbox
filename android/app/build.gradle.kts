import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "app.grabbox"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.grabbox"
        // youtubedl-android 0.18.1 requires Android 7.0/API 24.
        minSdk = 24
        targetSdk = 34
        versionCode = 300
        versionName = "0.3.0"
        vectorDrawables { useSupportLibrary = true }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }

    // The engine reads libpython.zip.so / libffmpeg.so straight out of
    // nativeLibraryDir during init(). AGP 8 stores .so files uncompressed and
    // skips the install-time extraction, so that directory comes up empty and
    // init() dies with a bare "failed to initialize". Legacy packaging puts
    // them back on disk where the engine can find them.
    packaging {
        jniLibs {
            useLegacyPackaging = true
        }
    }

    // One APK per ABI: the bundled Python/ffmpeg binaries are big, so nobody
    // downloads 3x what their phone needs.
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a", "x86_64")
            isUniversalApk = false
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-compose:1.9.2")

    implementation(platform("androidx.compose:compose-bom:2024.09.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")
    debugImplementation("androidx.compose.ui:ui-tooling")

    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.5")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.5")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")

    // The engine: yt-dlp + Python runtime + ffmpeg, bundled in the APK.
    implementation("io.github.junkfood02.youtubedl-android:library:0.18.1")
    implementation("io.github.junkfood02.youtubedl-android:ffmpeg:0.18.1")

    implementation("io.coil-kt:coil-compose:2.6.0")
}
