package app.hermesmobile.security

import java.nio.file.Files
import java.nio.file.Path
import java.util.Comparator
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.io.path.extension
import kotlin.io.path.isRegularFile
import kotlin.io.path.readText
import kotlin.streams.asSequence
import org.junit.Assert.assertTrue
import org.junit.Test

class PackagedSecretBoundaryTest {
    @Test
    fun `production sources cannot inject a password into packaged constants`() {
        val root = projectRoot()
        val buildScript = root.resolve("app/build.gradle.kts")
        val productionSources = sequenceOf(
            root.resolve("app/src/main"),
            root.resolve("app/src/debug"),
        ).flatMap(::sourceFiles)
        val files = sequenceOf(buildScript) + productionSources
        val forbiddenTerms = listOf(
            "DEBUG_INITIAL_" + "PASSWORD",
            "debugInitial" + "Password",
            "hermesMobileDebug" + "PasswordFile",
            "HERMES_MOBILE_DEBUG_" + "PASSWORD_FILE",
            "mobile-edge-initial-" + "password.txt",
        )

        val violatingFiles = files
            .filter { file ->
                val source = file.readText()
                forbiddenTerms.any(source::contains) ||
                    packagedSensitiveField.containsMatchIn(source)
            }
            .map { root.relativize(it).toString() }
            .distinct()
            .sorted()
            .toList()

        assertTrue(
            "Production sources still define packaged secret injection in: " +
                violatingFiles.joinToString(),
            violatingFiles.isEmpty(),
        )
    }

    @Test
    fun `archive scanner detects an early forbidden marker in every package format`() {
        val root = projectRoot()
        val fixtureRoot = root.resolve("app/build/intermediates/secretScan/debug")
        val forbiddenMarker = "DEBUG_INITIAL_" + "PASSWORD"
        val archiveNames = listOf(
            "early-marker.apk",
            "early-marker.aab",
            "early-marker.jar",
            "early-marker.zip",
        )
        Files.createDirectories(fixtureRoot)

        try {
            val fakeBin = fixtureRoot.resolve("fake-bin")
            val realUnzip = locateExecutable("unzip")
            val realPerl = locateExecutable("perl")
            installSigpipeUnzipWrapper(fakeBin)
            archiveNames.forEach { archiveName ->
                writeEarlyMarkerArchive(
                    path = fixtureRoot.resolve(archiveName),
                    forbiddenMarker = forbiddenMarker,
                )
            }

            val process = ProcessBuilder(
                "bash",
                root.resolve("scripts/verify-no-packaged-secrets.sh").toString(),
            )
                .directory(root.toFile())
                .redirectErrorStream(true)
                .also { builder ->
                    builder.environment()["REAL_UNZIP"] = realUnzip
                    builder.environment()["REAL_PERL"] = realPerl
                    builder.environment()["PATH"] =
                        "$fakeBin:${builder.environment().getValue("PATH")}"
                }
                .start()
            val output = process.inputStream.bufferedReader().use { it.readText() }
            val exitCode = process.waitFor()

            assertTrue("The scanner must reject every packaged marker.", exitCode != 0)
            assertTrue("Scanner diagnostics must not expose the marker.", forbiddenMarker !in output)
            archiveNames.forEach { archiveName ->
                assertTrue(
                    "Scanner did not report $archiveName. Output: $output",
                    archiveName in output,
                )
            }
        } finally {
            Files.walk(fixtureRoot).use { paths ->
                paths.sorted(Comparator.reverseOrder()).forEach { path ->
                    Files.deleteIfExists(path)
                }
            }
        }
    }

    private fun projectRoot(): Path {
        var candidate = Path.of(System.getProperty("user.dir")).toAbsolutePath()
        while (candidate.parent != null) {
            if (candidate.resolve("settings.gradle.kts").isRegularFile() &&
                candidate.resolve("app/build.gradle.kts").isRegularFile()
            ) {
                return candidate
            }
            candidate = candidate.parent
        }
        error("Hermes Android project root is unavailable")
    }

    private fun sourceFiles(directory: Path): Sequence<Path> {
        if (!Files.isDirectory(directory)) return emptySequence()
        return Files.walk(directory).use { paths ->
            paths.asSequence()
                .filter(Path::isRegularFile)
                .filter { it.extension == "kt" || it.extension == "kts" }
                .toList()
                .asSequence()
        }
    }

    private fun writeEarlyMarkerArchive(
        path: Path,
        forbiddenMarker: String,
    ) {
        ZipOutputStream(Files.newOutputStream(path)).use { archive ->
            archive.putNextEntry(ZipEntry("payload.bin"))
            archive.write("$forbiddenMarker\n".toByteArray())
            val padding = ByteArray(64 * 1_024)
            repeat(128) {
                archive.write(padding)
            }
            archive.closeEntry()
        }
    }

    private fun locateExecutable(name: String): String {
        val process = ProcessBuilder("sh", "-c", "command -v $name")
            .redirectErrorStream(true)
            .start()
        val output = process.inputStream.bufferedReader().use { it.readText() }.trim()
        check(process.waitFor() == 0 && output.isNotBlank()) {
            "Required executable is unavailable: $name"
        }
        return output
    }

    private fun installSigpipeUnzipWrapper(fakeBin: Path) {
        Files.createDirectories(fakeBin)
        val wrapper = fakeBin.resolve("unzip")
        Files.write(
            wrapper,
            """
            |#!/usr/bin/env bash
            |if [[ "${'$'}{1:-}" != "-p" || "${'$'}{2:-}" != *"/secretScan/"* ]]; then
            |  exec "${'$'}REAL_UNZIP" "${'$'}@"
            |fi
            |"${'$'}REAL_UNZIP" "${'$'}@"
            |extract_status=${'$'}?
            |if (( extract_status != 0 )); then
            |  exit "${'$'}extract_status"
            |fi
            |"${'$'}REAL_PERL" -e '${'$'}SIG{PIPE}=sub { exit 141 }; my ${'$'}chunk = "x" x 65536; for (1..128) { print ${'$'}chunk or exit 141; }'
            """.trimMargin().toByteArray(),
        )
        check(wrapper.toFile().setExecutable(true)) {
            "Unable to make the unzip test wrapper executable."
        }
    }

    private companion object {
        val packagedSensitiveField = Regex(
            """(?is)(buildConfigField|resValue)\s*\([^)]{0,240}""" +
                """(password|passphrase|secret|token|credential)""",
        )
    }
}
