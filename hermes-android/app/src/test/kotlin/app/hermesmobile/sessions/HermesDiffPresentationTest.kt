package app.hermesmobile.sessions

import androidx.compose.ui.graphics.Color
import kotlin.test.Test
import kotlin.test.assertEquals

class HermesDiffPresentationTest {
    @Test
    fun `unified diff lines receive semantic syntax colors`() {
        val addition = Color(0xFF157A35)
        val deletion = Color(0xFFC14240)
        val hunk = Color(0xFF245EB5)
        val fileHeader = Color(0xFF6E6E6E)
        val source = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n context\n+added\n-removed"

        val annotated = buildHermesDiffAnnotatedString(
            text = source,
            additionColor = addition,
            deletionColor = deletion,
            hunkColor = hunk,
            fileHeaderColor = fileHeader,
        )

        assertEquals(source, annotated.text)
        assertEquals(
            listOf(
                fileHeader to "--- a/file",
                fileHeader to "+++ b/file",
                hunk to "@@ -1 +1 @@",
                addition to "+added",
                deletion to "-removed",
            ),
            annotated.spanStyles.map { range ->
                range.item.color to annotated.text.substring(range.start, range.end)
            },
        )
    }

    @Test
    fun `diff and patch fenced code reuse semantic diff presentation`() {
        val addition = Color(0xFF157A35)
        val deletion = Color(0xFFC14240)
        val hunk = Color(0xFF245EB5)
        val fileHeader = Color(0xFF6E6E6E)
        val source = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n+added\n-removed"

        listOf("diff", "DIFF", "patch").forEach { language ->
            val annotated = buildHermesCodeFenceAnnotatedString(
                language = language,
                code = source,
                additionColor = addition,
                deletionColor = deletion,
                hunkColor = hunk,
                fileHeaderColor = fileHeader,
            )

            assertEquals(source, annotated.text)
            assertEquals(5, annotated.spanStyles.size)
        }

        val plain = buildHermesCodeFenceAnnotatedString(
            language = "kotlin",
            code = "+not a diff",
            additionColor = addition,
            deletionColor = deletion,
            hunkColor = hunk,
            fileHeaderColor = fileHeader,
        )
        assertEquals(emptyList(), plain.spanStyles)
    }
}
