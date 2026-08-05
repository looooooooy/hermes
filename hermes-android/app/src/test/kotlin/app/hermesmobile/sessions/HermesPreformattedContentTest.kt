package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class HermesPreformattedContentTest {
    @Test
    fun `preformatted content preserves explicit lines and uses local horizontal overflow`() {
        val source = "val response = buildHermesResponseWithA deliberatelyLongIdentifier()\nreturn response"

        val presentation = presentHermesPreformattedContent(
            source = source,
            kind = HermesPreformattedContentKind.CODE,
        )

        assertEquals(source, presentation.source)
        assertEquals(2, presentation.explicitLineCount)
        assertFalse(presentation.softWrap)
        assertTrue(presentation.scrollsHorizontally)
    }

    @Test
    fun `all professional preformatted kinds share the no-wrap contract`() {
        HermesPreformattedContentKind.entries.forEach { kind ->
            val presentation = presentHermesPreformattedContent(
                source = "one very long source line",
                kind = kind,
            )

            assertEquals(kind, presentation.kind)
            assertFalse(presentation.softWrap)
            assertTrue(presentation.scrollsHorizontally)
        }
    }

    @Test
    fun `tool output and commands default visible while code and diff stay collapsed`() {
        assertFalse(HermesPreformattedContentKind.CODE.defaultExpanded)
        assertFalse(HermesPreformattedContentKind.DIFF.defaultExpanded)
        assertTrue(HermesPreformattedContentKind.TOOL_OUTPUT.defaultExpanded)
        assertTrue(HermesPreformattedContentKind.COMMAND.defaultExpanded)
    }
}
