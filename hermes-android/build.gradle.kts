plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.kotlin.compose) apply false
}

tasks.register<Exec>("verifyNoPackagedSecrets") {
    group = "verification"
    description = "Builds debug/release artifacts and rejects packaged secret material."
    dependsOn(":app:assembleDebug", ":app:assembleRelease")
    commandLine("bash", layout.projectDirectory.file("scripts/verify-no-packaged-secrets.sh"))
}
