package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class HermesMarkdownParserTest {
    @Test
    fun `headings paragraphs and hard line breaks stay structured`() {
        val blocks = HermesMarkdownParser.parse(
            "## Live answer\n\nfirst line\nsecond line",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Heading(level = 2, text = "Live answer"),
                HermesMarkdownBlock.Paragraph(text = "first line\nsecond line"),
            ),
            blocks,
        )
    }

    @Test
    fun `setext underlines preserve level one and level two headings`() {
        val blocks = HermesMarkdownParser.parse(
            "Primary\n=======\n\nSecondary\n---",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Heading(level = 1, text = "Primary"),
                HermesMarkdownBlock.Heading(level = 2, text = "Secondary"),
            ),
            blocks,
        )
    }

    @Test
    fun `footnote definitions retain labels and indented continuation text`() {
        val blocks = HermesMarkdownParser.parse(
            "Claim[^source-1]\n\n[^source-1]: First line\n  continued detail",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Paragraph("Claim[^source-1]"),
                HermesMarkdownBlock.FootnoteDefinition(
                    label = "source-1",
                    text = "First line\ncontinued detail",
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `audio voice directive is hidden while following media remains structured`() {
        assertEquals(
            listOf(HermesMarkdownBlock.Media("/tmp/voice.ogg")),
            HermesMarkdownParser.parse("[[audio_as_voice]]\nMEDIA:/tmp/voice.ogg"),
        )
    }

    @Test
    fun `complete fenced code keeps language and exact internal newlines`() {
        val blocks = HermesMarkdownParser.parse(
            "before\n\n```kotlin\nval one = 1\nval two = 2\n```\n\nafter",
        )

        assertEquals(HermesMarkdownBlock.Paragraph("before"), blocks[0])
        assertEquals(
            HermesMarkdownBlock.CodeFence(
                language = "kotlin",
                code = "val one = 1\nval two = 2",
                isComplete = true,
            ),
            blocks[1],
        )
        assertEquals(HermesMarkdownBlock.Paragraph("after"), blocks[2])
    }

    @Test
    fun `long code fence keeps full opener and rejects shorter or mismatched closers`() {
        val blocks = HermesMarkdownParser.parse(
            "````kotlin\none\n~~~\n```\ntwo\n````",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.CodeFence(
                    language = "kotlin",
                    code = "one\n~~~\n```\ntwo",
                    isComplete = true,
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `code fence closer requires only whitespace after the full marker`() {
        val blocks = HermesMarkdownParser.parse(
            "~~~~text\none\n~~~~ payload\ntwo\n~~~~   \t\n\nAfter",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.CodeFence(
                    language = "text",
                    code = "one\n~~~~ payload\ntwo",
                    isComplete = true,
                ),
                HermesMarkdownBlock.Paragraph("After"),
            ),
            blocks,
        )
    }

    @Test
    fun `unfinished streaming fence renders as code instead of leaking fence markers`() {
        val blocks = HermesMarkdownParser.parse("```json\n{\n  \"live\": true")

        val code = blocks.single() as HermesMarkdownBlock.CodeFence
        assertEquals("json", code.language)
        assertEquals("{\n  \"live\": true", code.code)
        assertFalse(code.isComplete)
    }

    @Test
    fun `adjacent bullets become one list and quote remains separate`() {
        val blocks = HermesMarkdownParser.parse(
            "- first\n* second\n\n> important\n> still important",
        )

        assertEquals(
            HermesMarkdownBlock.BulletList(listOf("first", "second")),
            blocks[0],
        )
        assertEquals(
            HermesMarkdownBlock.Quote("important\nstill important"),
            blocks[1],
        )
    }

    @Test
    fun `nested unordered lists preserve logical depth`() {
        val blocks = HermesMarkdownParser.parse(
            "- parent\n  - child\n    * grandchild\n- sibling",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.BulletList(
                    items = listOf("parent", "child", "grandchild", "sibling"),
                    structuredItems = listOf(
                        HermesMarkdownListItem("parent", depth = 0),
                        HermesMarkdownListItem("child", depth = 1),
                        HermesMarkdownListItem("grandchild", depth = 2),
                        HermesMarkdownListItem("sibling", depth = 0),
                    ),
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `task list items preserve checked state separately from text`() {
        val blocks = HermesMarkdownParser.parse(
            "- [ ] queued\n  - [x] nested complete\n- ordinary",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.BulletList(
                    items = listOf("[ ] queued", "[x] nested complete", "ordinary"),
                    structuredItems = listOf(
                        HermesMarkdownListItem(
                            text = "queued",
                            depth = 0,
                            taskState = HermesMarkdownTaskState.UNCHECKED,
                        ),
                        HermesMarkdownListItem(
                            text = "nested complete",
                            depth = 1,
                            taskState = HermesMarkdownTaskState.CHECKED,
                        ),
                        HermesMarkdownListItem(
                            text = "ordinary",
                            depth = 0,
                        ),
                    ),
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `nested block quotes preserve depth for every source line`() {
        val blocks = HermesMarkdownParser.parse(
            "> outer\n>> inner\n> > > deepest\n> outer again",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Quote(
                    text = "outer\n> inner\n> > deepest\nouter again",
                    structuredLines = listOf(
                        HermesMarkdownQuoteLine("outer", depth = 1),
                        HermesMarkdownQuoteLine("inner", depth = 2),
                        HermesMarkdownQuoteLine("deepest", depth = 3),
                        HermesMarkdownQuoteLine("outer again", depth = 1),
                    ),
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `adjacent numbered items become one ordered list`() {
        val blocks = HermesMarkdownParser.parse(
            "1. Inspect the source\n2. Run the tests",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.OrderedList(
                    items = listOf("Inspect the source", "Run the tests"),
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `mixed ordered and unordered nesting preserves kind depth and ordinal`() {
        val blocks = HermesMarkdownParser.parse(
            "3. parent\n   - bullet child\n     8) ordered grandchild\n4. sibling",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.OrderedList(
                    items = listOf("parent", "bullet child", "ordered grandchild", "sibling"),
                    structuredItems = listOf(
                        HermesMarkdownListItem(
                            text = "parent",
                            depth = 0,
                            kind = HermesMarkdownListKind.ORDERED,
                            ordinal = 3,
                        ),
                        HermesMarkdownListItem(
                            text = "bullet child",
                            depth = 1,
                            kind = HermesMarkdownListKind.UNORDERED,
                        ),
                        HermesMarkdownListItem(
                            text = "ordered grandchild",
                            depth = 2,
                            kind = HermesMarkdownListKind.ORDERED,
                            ordinal = 8,
                        ),
                        HermesMarkdownListItem(
                            text = "sibling",
                            depth = 0,
                            kind = HermesMarkdownListKind.ORDERED,
                            ordinal = 4,
                        ),
                    ),
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `horizontal rule stays separate from surrounding paragraphs`() {
        val blocks = HermesMarkdownParser.parse("before\n\n---\n\nafter")

        assertEquals(
            listOf(
                HermesMarkdownBlock.Paragraph("before"),
                HermesMarkdownBlock.HorizontalRule,
                HermesMarkdownBlock.Paragraph("after"),
            ),
            blocks,
        )
    }

    @Test
    fun `pipe table keeps headers and aligned rows`() {
        val blocks = HermesMarkdownParser.parse(
            "| Tool | Status |\n| --- | :---: |\n| terminal | Running |\n| browser | Complete |",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Table(
                    headers = listOf("Tool", "Status"),
                    rows = listOf(
                        listOf("terminal", "Running"),
                        listOf("browser", "Complete"),
                    ),
                ),
            ),
            blocks,
        )
    }

    @Test
    fun `display math waits for the closer matching its opener`() {
        val blocks = HermesMarkdownParser.parse(
            "Before\n\n\\[\nx\n$$\ny\n\\]\n\nAfter",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Paragraph("Before"),
                HermesMarkdownBlock.DisplayMath("x\n$$\ny"),
                HermesMarkdownBlock.Paragraph("After"),
            ),
            blocks,
        )
    }

    @Test
    fun `unmatched display math falls back to prose without swallowing later blocks`() {
        val blocks = HermesMarkdownParser.parse(
            "Before\n\n$$\nx + y\n# Following\n\n- item",
        )

        assertEquals(
            listOf(
                HermesMarkdownBlock.Paragraph("Before"),
                HermesMarkdownBlock.Paragraph("$$\nx + y"),
                HermesMarkdownBlock.Heading(level = 1, text = "Following"),
                HermesMarkdownBlock.BulletList(listOf("item")),
            ),
            blocks,
        )
    }

    @Test
    fun `blank input produces no visual blocks`() {
        assertTrue(HermesMarkdownParser.parse(" \n\n ").isEmpty())
    }
}
