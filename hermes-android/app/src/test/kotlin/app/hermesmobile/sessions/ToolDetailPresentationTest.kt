package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class ToolDetailPresentationTest {
    @Test
    fun `short tool detail stays complete without an expand affordance`() {
        val presentation = toolDetailPresentation(
            body = "line 1\nline 2",
            expanded = false,
        )

        assertEquals("line 1\nline 2", presentation.visibleBody)
        assertFalse(presentation.isTruncated)
        assertFalse(presentation.canExpand)
    }

    @Test
    fun `long tool detail is bounded until explicitly expanded`() {
        val body = (1..40).joinToString("\n") { "output line $it" }

        val collapsed = toolDetailPresentation(body = body, expanded = false)
        assertTrue(collapsed.isTruncated)
        assertTrue(collapsed.canExpand)
        assertTrue(collapsed.visibleBody.startsWith("output line 1\n"))
        assertFalse(collapsed.visibleBody.contains("output line 40"))

        val expanded = toolDetailPresentation(body = body, expanded = true)
        assertEquals(body, expanded.visibleBody)
        assertFalse(expanded.isTruncated)
        assertTrue(expanded.canExpand)
    }

    @Test
    fun `large single-line output is bounded on a unicode code point boundary`() {
        val body = "🙂".repeat(5_000)

        val collapsed = toolDetailPresentation(body = body, expanded = false)

        assertTrue(collapsed.isTruncated)
        assertEquals(
            TOOL_DETAIL_PREVIEW_MAX_CODE_POINTS,
            collapsed.visibleBody.codePointCount(0, collapsed.visibleBody.length),
        )
        assertTrue(
            Character.isSurrogatePair(
                collapsed.visibleBody[collapsed.visibleBody.lastIndex - 1],
                collapsed.visibleBody.last(),
            ),
        )
    }
}
