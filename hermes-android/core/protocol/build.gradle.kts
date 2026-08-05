import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

kotlin {
    jvmToolchain(21)
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

val generatedObserverAuthorityDir = layout.buildDirectory.dir(
    "generated/sources/observerAuthority/kotlin",
)
val generateObserverV2Authority by tasks.registering {
    val authorityFile = layout.projectDirectory.file(
        "src/test/resources/contracts/observer-output-parity-v2.json",
    )
    val outputFile = generatedObserverAuthorityDir.map {
        it.file("app/hermesmobile/protocol/gateway/GeneratedObserverV2Authority.kt")
    }
    inputs.file(authorityFile)
    outputs.file(outputFile)
    doLast {
        val document = authorityFile.asFile.readText()
        fun stringArray(field: String): List<String> {
            val body = Regex(
                "\\\"$field\\\"\\s*:\\s*\\[(.*?)]",
                setOf(RegexOption.DOT_MATCHES_ALL),
            ).find(document)?.groupValues?.get(1)
                ?: error("Missing $field in generated observer authority")
            return Regex("\\\"([^\\\"]+)\\\"")
                .findAll(body)
                .map { match -> match.groupValues[1] }
                .toList()
        }
        val events = stringArray("event_types")
        val lifecycle = stringArray("non_mergeable_lifecycle_event_types")
        val target = outputFile.get().asFile
        target.parentFile.mkdirs()
        target.writeText(
            buildString {
                appendLine("package app.hermesmobile.protocol.gateway")
                appendLine()
                appendLine("// Generated from the synchronized root observer-output-parity-v2 authority.")
                appendLine("internal object GeneratedObserverV2Authority {")
                appendLine("    val eventTypes: Set<String> = setOf(")
                events.forEach { appendLine("        \"$it\",") }
                appendLine("    )")
                appendLine("    val lifecycleEventTypes: Set<String> = setOf(")
                lifecycle.forEach { appendLine("        \"$it\",") }
                appendLine("    )")
                appendLine("}")
            },
        )
    }
}

kotlin.sourceSets.named("main") {
    kotlin.srcDir(generatedObserverAuthorityDir)
}

tasks.named("compileKotlin") {
    dependsOn(generateObserverV2Authority)
}

dependencies {
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.okhttp)

    testImplementation(kotlin("test-junit"))
    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.okhttp.tls)
}

tasks.test {
    useJUnit()
}
