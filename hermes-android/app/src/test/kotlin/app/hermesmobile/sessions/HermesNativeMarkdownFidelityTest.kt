package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class HermesNativeMarkdownFidelityTest {
    @Test
    fun `every streaming cut point reconstructs and parses like one shot markdown`() {
        val markdown = """
            # Report

            Paragraph with **bold**, [docs](https://example.com), and ${'$'}x + y${'$'}.

            - parent
              - [x] nested task

            > quote
            > continued

            | Name | State |
            | --- | --- |
            | Hermes | live |

            ````kotlin
            val fence = "```"
            ````

            ${'$'}${'$'}
            x^2 + y^2
            ${'$'}${'$'}

            [^1]: Footnote text

            MEDIA:https://example.com/chart.png
        """.trimIndent()
        val scanner = HermesStreamingMarkdownScanner()

        for (cut in 0..markdown.length) {
            val prefix = markdown.substring(0, cut)
            val snapshot = scanner.advance(prefix)

            assertEquals(prefix, snapshot.settled + snapshot.tail, "reconstruction at cut $cut")
            assertEquals(
                HermesMarkdownParser.parse(prefix),
                snapshot.settledBlocks.flatMap(HermesMarkdownParser::parse) +
                    HermesMarkdownParser.parse(snapshot.tail),
                "parser equivalence at cut $cut",
            )
        }
    }

    @Test
    fun `incremental scanner appends settled blocks and keeps the live tail`() {
        val scanner = HermesStreamingMarkdownScanner()

        val first = scanner.advance("First block\n\nSecond")
        assertEquals(listOf("First block\n\n"), first.settledBlocks)
        assertEquals("Second", first.tail)

        val second = scanner.advance("First block\n\nSecond block\n\nThird")
        assertEquals(
            listOf("First block\n\n", "Second block\n\n"),
            second.settledBlocks,
        )
        assertEquals("Third", second.tail)
    }

    @Test
    fun `incremental scanner settles at whitespace-only CRLF blank lines`() {
        val scanner = HermesStreamingMarkdownScanner()

        val snapshot = scanner.advance("First\r\n \t\r\nSecond")

        assertEquals(listOf("First\r\n \t\r\n"), snapshot.settledBlocks)
        assertEquals("Second", snapshot.tail)
    }

    @Test
    fun `incremental scanner requires matching fence character and opener length`() {
        val scanner = HermesStreamingMarkdownScanner()
        val beforeClose = "Before\n\n````kotlin\ncode\n\n~~~\n```\n"

        val open = scanner.advance(beforeClose)
        assertEquals(listOf("Before\n\n"), open.settledBlocks)
        assertEquals("````kotlin\ncode\n\n~~~\n```\n", open.tail)

        val closed = scanner.advance("${beforeClose}`````\n\nAfter")
        assertEquals(
            listOf(
                "Before\n\n",
                "````kotlin\ncode\n\n~~~\n```\n`````\n\n",
            ),
            closed.settledBlocks,
        )
        assertEquals("After", closed.tail)
    }

    @Test
    fun `incremental scanner matches display math closer to its opener`() {
        val scanner = HermesStreamingMarkdownScanner()
        val beforeClose = "Before\n\n\\[\nx\n\n$$\n"

        val open = scanner.advance(beforeClose)
        assertEquals(listOf("Before\n\n"), open.settledBlocks)
        assertEquals("\\[\nx\n\n$$\n", open.tail)

        val closed = scanner.advance("${beforeClose}\\]\n\nAfter")
        assertEquals(
            listOf(
                "Before\n\n",
                "\\[\nx\n\n$$\n\\]\n\n",
            ),
            closed.settledBlocks,
        )
        assertEquals("After", closed.tail)
    }

    @Test
    fun `incremental scanner ignores math delimiters inside code fences`() {
        val scanner = HermesStreamingMarkdownScanner()
        val text = "Before\n\n```text\n$$\n\n```\n\nAfter"

        val snapshot = scanner.advance(text)

        assertEquals(
            listOf(
                "Before\n\n",
                "```text\n$$\n\n```\n\n",
            ),
            snapshot.settledBlocks,
        )
        assertEquals("After", snapshot.tail)
    }

    @Test
    fun `incremental scanner treats fence-looking lines inside display math as math content`() {
        val scanner = HermesStreamingMarkdownScanner()
        val text = "Before\n\n$$\n``` literal math content\n$$\n\nAfter"

        val snapshot = scanner.advance(text)

        assertEquals(
            listOf(
                "Before\n\n",
                "$$\n``` literal math content\n$$\n\n",
            ),
            snapshot.settledBlocks,
        )
        assertEquals("After", snapshot.tail)
    }

    @Test
    fun `incremental scanner does not advance state for a partial line`() {
        val scanner = HermesStreamingMarkdownScanner()
        val completePrefix = "Before\n\n````\ncode\n"

        scanner.advance("${completePrefix}```")
        val revisedPartialLine = scanner.advance("${completePrefix}not a closer\n\nAfter")

        assertEquals(listOf("Before\n\n"), revisedPartialLine.settledBlocks)
        assertEquals("````\ncode\nnot a closer\n\nAfter", revisedPartialLine.tail)
    }

    @Test
    fun `incremental scanner resets when text stops extending the scanned prefix`() {
        val scanner = HermesStreamingMarkdownScanner()
        scanner.advance("Old\n\nTail")

        val replacement = scanner.advance("New\n\nTail")

        assertEquals(listOf("New\n\n"), replacement.settledBlocks)
        assertEquals("Tail", replacement.tail)
    }

    @Test
    fun `replacement after open fence and math state matches a fresh scanner`() {
        val reused = HermesStreamingMarkdownScanner()
        reused.advance("Old\n\n````kotlin\ncode\n$$\n")
        val replacement = "New\n\n${'$'}${'$'}\nx + y\n${'$'}${'$'}\n\nTail"

        val reusedSnapshot = reused.advance(replacement)
        val freshSnapshot = HermesStreamingMarkdownScanner().advance(replacement)

        assertEquals(freshSnapshot, reusedSnapshot)
    }

    @Test
    fun `incremental scanner is idempotent for repeated identical input`() {
        val scanner = HermesStreamingMarkdownScanner()
        val text = "First\n\nSecond\n\nTail"

        val first = scanner.advance(text)
        val repeated = scanner.advance(text)

        assertEquals(first, repeated)
        assertEquals(listOf("First\n\n", "Second\n\n"), repeated.settledBlocks)
    }

    @Test
    fun `long append stream processes each complete line only once`() {
        val scanner = HermesStreamingMarkdownScanner()
        val text = StringBuilder()

        repeat(200) { index ->
            text.append("Block ").append(index).append("\n\n")
            scanner.advance(text.toString())
        }

        assertEquals(400L, scanner.processedCompleteLineCount)
    }

    @Test
    fun `tilde fences are parsed as code blocks like Hermes terminal markdown`() {
        val blocks = HermesMarkdownParser.parse("Intro\n~~~python\nprint('hi')\n~~~\nOutro")

        assertEquals(
            listOf(
                HermesMarkdownBlock.Paragraph("Intro"),
                HermesMarkdownBlock.CodeFence(
                    language = "python",
                    code = "print('hi')",
                    isComplete = true,
                ),
                HermesMarkdownBlock.Paragraph("Outro"),
            ),
            blocks,
        )
    }

    @Test
    fun `display math blocks are parsed as native Hermes math content`() {
        val blocks = HermesMarkdownParser.parse("Before\n$$\\mathbb{R}^2$$\nAfter")

        assertEquals(
            listOf(
                HermesMarkdownBlock.Paragraph("Before"),
                HermesMarkdownBlock.DisplayMath("\\mathbb{R}^2"),
                HermesMarkdownBlock.Paragraph("After"),
            ),
            blocks,
        )
    }

    @Test
    fun `media directive stays a native media block instead of markdown prose`() {
        val blocks = HermesMarkdownParser.parse("MEDIA:/tmp/chart.png")

        assertEquals(
            listOf(HermesMarkdownBlock.Media("/tmp/chart.png")),
            blocks,
        )
    }

    @Test
    fun `streaming split does not commit the middle of a fenced code block`() {
        val text = "Answer\n\n```python\nprint(1)\nprint(2)\n"
        val split = HermesStreamingMarkdown.splitStableBoundary(text)

        assertEquals("Answer\n\n", split.settled)
        assertEquals("```python\nprint(1)\nprint(2)\n", split.tail)
        assertIs<HermesMarkdownBlock.Paragraph>(HermesMarkdownParser.parse(split.settled).single())
    }

    @Test
    fun `streaming split uses production matching fence semantics`() {
        val text = "Before\n\n````kotlin\ncode\n```\n\nAfter"

        val split = HermesStreamingMarkdown.splitStableBoundary(text)

        assertEquals("Before\n\n", split.settled)
        assertEquals("````kotlin\ncode\n```\n\nAfter", split.tail)
    }

    @Test
    fun `streaming split does not split an unclosed display math block`() {
        val text = "Before\n\n$$\\sum_i x_i\n+ y_i\n"
        val split = HermesStreamingMarkdown.splitStableBoundary(text)

        assertEquals("Before\n\n", split.settled)
        assertEquals("$$\\sum_i x_i\n+ y_i\n", split.tail)
    }
}
