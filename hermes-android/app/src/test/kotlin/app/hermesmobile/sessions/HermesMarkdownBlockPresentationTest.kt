package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals

class HermesMarkdownBlockPresentationTest {
    @Test
    fun `structured list rows preserve depth kind ordinal and task state`() {
        val rows = presentHermesMarkdownListItems(
            listOf(
                HermesMarkdownListItem(
                    text = "parent",
                    depth = 0,
                    kind = HermesMarkdownListKind.ORDERED,
                    ordinal = 3,
                ),
                HermesMarkdownListItem(
                    text = "queued",
                    depth = 1,
                    taskState = HermesMarkdownTaskState.UNCHECKED,
                ),
                HermesMarkdownListItem(
                    text = "done",
                    depth = 1,
                    taskState = HermesMarkdownTaskState.CHECKED,
                ),
            ),
        )

        assertEquals(
            listOf(
                HermesMarkdownListRow(marker = "3.", text = "parent", depth = 0),
                HermesMarkdownListRow(marker = "☐", text = "queued", depth = 1),
                HermesMarkdownListRow(marker = "☑", text = "done", depth = 1),
            ),
            rows,
        )
    }

    @Test
    fun `structured quote rows preserve native rail depth without glyph indentation`() {
        val rows = presentHermesMarkdownQuoteLines(
            listOf(
                HermesMarkdownQuoteLine(text = "outer", depth = 1),
                HermesMarkdownQuoteLine(text = "inner", depth = 2),
            ),
        )

        assertEquals(
            listOf(
                HermesMarkdownQuoteRow(text = "outer", railDepth = 1),
                HermesMarkdownQuoteRow(text = "inner", railDepth = 2),
            ),
            rows,
        )
    }

    @Test
    fun `media presentation identifies supported types and exposes only safe remote targets`() {
        assertEquals(
            HermesMarkdownMediaPresentation(
                kind = HermesMarkdownMediaKind.IMAGE,
                label = "capture.png",
                openUrl = null,
            ),
            presentHermesMarkdownMedia("/Users/person/private/capture.png"),
        )
        assertEquals(
            HermesMarkdownMediaPresentation(
                kind = HermesMarkdownMediaKind.AUDIO,
                label = "voice.mp3",
                openUrl = "https://example.com/voice.mp3",
            ),
            presentHermesMarkdownMedia("https://example.com/voice.mp3"),
        )
        assertEquals(
            HermesMarkdownMediaPresentation(
                kind = HermesMarkdownMediaKind.AUDIO,
                label = "voice.mp3",
                openUrl = null,
            ),
            presentHermesMarkdownMedia(
                "https://example.com/voice.mp3?access_token=must-not-render",
            ),
        )
    }
}
