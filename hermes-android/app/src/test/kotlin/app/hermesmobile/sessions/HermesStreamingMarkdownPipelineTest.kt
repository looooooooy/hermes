package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotEquals
import kotlin.test.assertSame

class HermesStreamingMarkdownPipelineTest {
    @Test
    fun `every cut point matches one shot sanitizing and parsing`() {
        val markdown = "Before \u001B[31mred\u001B[0m\n\n```text\nopen"
        val pipeline = HermesStreamingMarkdownPipeline()

        for (cut in 0..markdown.length) {
            val prefix = markdown.substring(0, cut)
            val expectedSanitized = HermesMarkdownSanitizer.sanitize(prefix)
            val snapshot = pipeline.advance(prefix)

            assertEquals(expectedSanitized, snapshot.sanitizedMarkdown, "sanitizing at cut $cut")
            assertEquals(
                expectedSanitized,
                snapshot.settledMarkdown.joinToString(separator = "") + snapshot.tailMarkdown,
                "reconstruction at cut $cut",
            )
            assertEquals(
                HermesMarkdownParser.parse(expectedSanitized),
                snapshot.blocks,
                "parsing at cut $cut",
            )
        }
    }

    @Test
    fun `OSC 8 split at every boundary matches one shot sanitizing and parsing`() {
        assertEveryCutMatchesOneShot(
            "before \u001B]8;;https://example.com/docs\u001B\\docs\u001B]8;;\u001B\\ after",
        )
    }

    @Test
    fun `OSC payloads terminated by BEL match one shot at every boundary`() {
        assertEveryCutMatchesOneShot(
            "before \u001B]8;;https://example.com/docs\u0007docs\u001B]8;;\u0007 after",
        )
    }

    @Test
    fun `all terminal control strings remain hidden at every streaming cut`() {
        listOf(
            "\u001BP" to "\u001B\\",
            "\u001B_" to "\u001B\\",
            "\u001B^" to "\u001B\\",
            "\u001BX" to "\u001B\\",
            "\u0090" to "\u009C",
            "\u009F" to "\u009C",
            "\u009E" to "\u009C",
            "\u0098" to "\u009C",
        ).forEach { (introducer, terminator) ->
            assertControlStringHiddenAtEveryCut(introducer, terminator)
        }
    }

    @Test
    fun `terminal control restarts remain hidden at every streaming cut`() {
        listOf(
            "\u001B\u001B]8;;" to "\u0007",
            "\u001B[31\u001B]8;;" to "\u0007",
            "\u001B\u009D8;;" to "\u009C",
            "\u001B[31\u009D8;;" to "\u009C",
            "\u001B\u001BP" to "\u001B\\",
            "\u001B[31\u0090" to "\u009C",
        ).forEach { (introducer, terminator) ->
            assertControlStringHiddenAtEveryCut(introducer, terminator)
        }
    }

    @Test
    fun `C1 CSI remains hidden until its final byte at every streaming cut`() {
        val before = "before "
        val control = "\u009B31m"
        val visible = "red after"
        val markdown = before + control + visible
        val controlEnd = before.length + control.length
        val pipeline = HermesStreamingMarkdownPipeline()

        for (cut in 0..markdown.length) {
            val expected = if (cut <= controlEnd) {
                before.take(cut.coerceAtMost(before.length))
            } else {
                before + markdown.substring(controlEnd, cut)
            }
            assertEquals(expected, pipeline.advance(markdown.substring(0, cut)).sanitizedMarkdown)
        }
    }

    @Test
    fun `lone escapes delete and disallowed controls match one shot at every boundary`() {
        assertEveryCutMatchesOneShot(
            "before\u001B?dropped\u007F\u0000\u0001\tkept\r\nafter\u001B",
        )
    }

    @Test
    fun `CRLF fences and display math match one shot at every boundary`() {
        assertEveryCutMatchesOneShot(
            "Before\r\n\r\n````kotlin\r\nval fence = \"```\"\r\n````\r\n\r\n" +
                "\\[\r\nx + y\r\n\\]\r\n\r\nTail",
        )
    }

    @Test
    fun `CSI split across deltas is sanitized from append suffixes`() {
        val pipeline = HermesStreamingMarkdownPipeline()
        val prefixes = listOf(
            "before \u001B",
            "before \u001B[31",
            "before \u001B[31mred\u001B[",
            "before \u001B[31mred\u001B[0m after",
        )

        prefixes.forEach { prefix ->
            assertEquals(
                HermesMarkdownSanitizer.sanitize(prefix),
                pipeline.advance(prefix).sanitizedMarkdown,
            )
        }

        assertEquals(prefixes.last().length.toLong(), pipeline.processedSourceCharacterCount)
    }

    @Test
    fun `settled blocks are parsed once while the tail keeps streaming`() {
        val pipeline = HermesStreamingMarkdownPipeline()

        pipeline.advance("First\n\nTail")
        assertEquals(1L, pipeline.settledBlockParseCount)

        pipeline.advance("First\n\nTail grows")
        assertEquals(1L, pipeline.settledBlockParseCount)

        pipeline.advance("First\n\nTail grows\n\nNext")
        assertEquals(2L, pipeline.settledBlockParseCount)
    }

    @Test
    fun `incremental append work processes each sanitized character once`() {
        val pipeline = HermesStreamingMarkdownPipeline()
        val markdown = buildString {
            repeat(40) { index ->
                append("Block ").append(index).append(" grows\r\n\r\n")
            }
            append("live tail")
        }

        for (cut in 0..markdown.length) {
            pipeline.advance(markdown.substring(0, cut))
        }

        assertEquals(markdown.length.toLong(), pipeline.processedSanitizedCharacterCount)
    }

    @Test
    fun `repeated identical input performs no sanitizer scanner or settled parse work`() {
        val pipeline = HermesStreamingMarkdownPipeline()
        val markdown = "First\n\nSecond\n\nTail"
        val first = pipeline.advance(markdown)
        val sourceWork = pipeline.processedSourceCharacterCount
        val scannerWork = pipeline.processedSanitizedCharacterCount
        val parseWork = pipeline.settledBlockParseCount

        val repeated = pipeline.advance(markdown)

        assertSame(first, repeated)
        assertEquals(sourceWork, pipeline.processedSourceCharacterCount)
        assertEquals(scannerWork, pipeline.processedSanitizedCharacterCount)
        assertEquals(parseWork, pipeline.settledBlockParseCount)
    }

    @Test
    fun `non append replacement resets all pipeline state and matches a fresh pipeline`() {
        val reused = HermesStreamingMarkdownPipeline()
        reused.advance("Old\n\n````kotlin\ncode\n\u001B]8;;https://hidden.example")
        val replacement = "New\r\n\r\n$$\r\nx + y\r\n$$\r\n\r\nTail"

        val reusedSnapshot = reused.advance(replacement)
        val fresh = HermesStreamingMarkdownPipeline()
        val freshSnapshot = fresh.advance(replacement)

        assertEquals(freshSnapshot.sanitizedMarkdown, reusedSnapshot.sanitizedMarkdown)
        assertEquals(freshSnapshot.settledMarkdown, reusedSnapshot.settledMarkdown)
        assertEquals(freshSnapshot.tailMarkdown, reusedSnapshot.tailMarkdown)
        assertEquals(freshSnapshot.blocks, reusedSnapshot.blocks)
        assertNotEquals(freshSnapshot.generation, reusedSnapshot.generation)
        assertEquals(replacement.length.toLong(), reused.processedSourceCharacterCount)
        assertEquals(fresh.processedSanitizedCharacterCount, reused.processedSanitizedCharacterCount)
        assertEquals(fresh.settledBlockParseCount, reused.settledBlockParseCount)
    }

    @Test
    fun `tail segment identity survives settlement and replacement advances generation`() {
        val pipeline = HermesStreamingMarkdownPipeline()
        val live = pipeline.advance("```kotlin\nval answer = 42")
        val liveIdentity = live.tailSegmentId

        val settled = pipeline.advance("```kotlin\nval answer = 42\n```\n\nNext")

        assertEquals(liveIdentity, settled.settledSegmentIds.single())
        assertEquals(live.generation, settled.generation)
        assertNotEquals(liveIdentity, settled.tailSegmentId)

        val replacement = pipeline.advance("Unrelated replacement")
        assertNotEquals(settled.generation, replacement.generation)
    }

    @Test
    fun `snapshot parse lists cannot mutate pipeline cache`() {
        val pipeline = HermesStreamingMarkdownPipeline()
        val snapshot = pipeline.advance("First\n\nTail")

        assertFailsWith<UnsupportedOperationException> {
            @Suppress("UNCHECKED_CAST")
            (snapshot.parsedSettledBlocks.single() as MutableList<HermesMarkdownBlock>).clear()
        }
        assertEquals(snapshot.blocks, pipeline.advance("First\n\nTail").blocks)
    }

    private fun assertEveryCutMatchesOneShot(markdown: String) {
        val pipeline = HermesStreamingMarkdownPipeline()

        for (cut in 0..markdown.length) {
            val prefix = markdown.substring(0, cut)
            val expectedSanitized = HermesMarkdownSanitizer.sanitize(prefix)
            val snapshot = pipeline.advance(prefix)

            assertEquals(expectedSanitized, snapshot.sanitizedMarkdown, "sanitizing at cut $cut")
            assertEquals(
                expectedSanitized,
                snapshot.settledMarkdown.joinToString(separator = "") + snapshot.tailMarkdown,
                "reconstruction at cut $cut",
            )
            assertEquals(
                HermesMarkdownParser.parse(expectedSanitized),
                snapshot.blocks,
                "parsing at cut $cut",
            )
        }
    }

    private fun assertControlStringHiddenAtEveryCut(
        introducer: String,
        terminator: String,
    ) {
        val before = "before "
        val hidden = "[hidden](https://example.com)"
        val after = " after"
        val markdown = before + introducer + hidden + terminator + after
        val terminatorEnd = before.length + introducer.length + hidden.length + terminator.length
        val pipeline = HermesStreamingMarkdownPipeline()

        for (cut in 0..markdown.length) {
            val expected = when {
                cut <= before.length -> before.take(cut)
                cut <= terminatorEnd -> before
                else -> before + markdown.substring(terminatorEnd, cut)
            }
            assertEquals(
                expected,
                pipeline.advance(markdown.substring(0, cut)).sanitizedMarkdown,
                "cut=$cut introducer=${introducer.codePoints().toArray().contentToString()}",
            )
        }
    }
}
