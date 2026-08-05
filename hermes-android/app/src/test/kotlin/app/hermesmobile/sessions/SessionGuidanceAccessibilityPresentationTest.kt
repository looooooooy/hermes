package app.hermesmobile.sessions

import app.hermesmobile.R
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals

class SessionGuidanceAccessibilityPresentationTest {
    @Test
    fun `guidance accessibility distinguishes toggle state input and submission`() {
        val collapsed = guidanceAccessibilityPresentation(expanded = false)
        val expanded = guidanceAccessibilityPresentation(expanded = true)

        assertEquals(R.string.guidance_expand, collapsed.toggleLabelResource)
        assertEquals(R.string.guidance_collapsed, collapsed.toggleStateResource)
        assertEquals(R.string.guidance_collapse, expanded.toggleLabelResource)
        assertEquals(R.string.guidance_expanded, expanded.toggleStateResource)
        assertEquals(R.string.guidance_input_label, collapsed.inputLabelResource)
        assertEquals(R.string.guidance_submit_label, collapsed.submitLabelResource)
        assertNotEquals(collapsed.toggleLabelResource, collapsed.submitLabelResource)
    }
}
