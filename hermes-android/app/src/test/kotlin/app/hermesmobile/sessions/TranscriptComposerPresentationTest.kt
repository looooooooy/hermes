package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class TranscriptComposerPresentationTest {
    @Test
    fun `running composer actions fit a 320dp viewport`() {
        val availableWidthDp = 320 - (2 * 12) - (2 * 6)

        assertTrue(COMPOSER_VOICE_BUTTON_WIDTH_DP >= 64)
        assertTrue(
            composerActionRowMinimumWidthDp(
                guidanceActionVisible = true,
                stopActionVisible = true,
            ) <= availableWidthDp,
        )
    }

    @Test
    fun `running turn exposes Queue as the primary composer submission`() {
        assertEquals(
            TranscriptComposerPrimaryAction.Queue,
            transcriptComposerPrimaryAction(
                running = true,
                isInterrupting = false,
                guidanceMode = false,
            ),
        )
    }

    @Test
    fun `guide mode submits guidance from the same running composer`() {
        assertEquals(
            TranscriptComposerPrimaryAction.Guide,
            transcriptComposerPrimaryAction(
                running = true,
                isInterrupting = false,
                guidanceMode = true,
            ),
        )
    }

    @Test
    fun `idle composer always returns to ordinary send semantics`() {
        assertEquals(
            TranscriptComposerPrimaryAction.Send,
            transcriptComposerPrimaryAction(
                running = false,
                isInterrupting = false,
                guidanceMode = true,
            ),
        )
    }
}
