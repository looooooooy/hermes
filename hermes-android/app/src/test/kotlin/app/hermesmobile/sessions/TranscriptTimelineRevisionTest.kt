package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals
import kotlin.test.assertNotSame
import kotlin.test.assertSame

class TranscriptTimelineRevisionTest {
    @Test
    fun `realtime revision uses authoritative epoch and ordinal`() {
        val current = transcriptTimelineRevision(
            connectionEpoch = 4,
            lastEventOrdinal = 20,
            historyCount = 200,
            lastHistoryId = 999,
        )

        assertEquals(
            current,
            transcriptTimelineRevision(
                connectionEpoch = 4,
                lastEventOrdinal = 20,
                historyCount = 400,
                lastHistoryId = 1_999,
            ),
        )
        assertNotEquals(
            current,
            transcriptTimelineRevision(
                connectionEpoch = 4,
                lastEventOrdinal = 21,
                historyCount = 200,
                lastHistoryId = 999,
            ),
        )
    }

    @Test
    fun `rest baseline revision uses message count and tail id`() {
        assertNotEquals(
            transcriptTimelineRevision(
                connectionEpoch = null,
                lastEventOrdinal = null,
                historyCount = 20,
                lastHistoryId = 20,
            ),
            transcriptTimelineRevision(
                connectionEpoch = null,
                lastEventOrdinal = null,
                historyCount = 21,
                lastHistoryId = 21,
            ),
        )
    }

    @Test
    fun `rendered key cache reuses content-only structure and replaces changed structure`() {
        val cache = TranscriptRenderedItemKeysCache()
        val initial = cache.update(
            hasHistoryHeader = false,
            conversationTurns = listOf(turn(key = "current", response = "Partial")),
            legacyTurns = emptyList(),
        )

        val contentDelta = cache.update(
            hasHistoryHeader = false,
            conversationTurns = listOf(turn(key = "current", response = "Partial answer")),
            legacyTurns = emptyList(),
        )
        assertSame(initial, contentDelta)

        val prepended = cache.update(
            hasHistoryHeader = true,
            conversationTurns = listOf(
                turn(key = "older", response = "Older answer"),
                turn(key = "current", response = "Partial answer"),
            ),
            legacyTurns = emptyList(),
        )
        assertNotSame(initial, prepended)
        assertEquals(listOf("load-older-messages", "older", "current"), prepended)
    }

    @Test
    fun `scroll anchor tracker preserves surviving key and only retains its own offset`() {
        val tracker = TranscriptScrollAnchorTracker()
        tracker.update(
            renderedItemKeys = listOf("older", "current", "latest"),
            firstVisibleIndex = 1,
            firstVisibleScrollOffset = 23,
        )

        assertEquals(
            TranscriptScrollAnchor(key = "current", scrollOffset = 23),
            tracker.anchorFor(listOf("oldest", "older", "current", "latest")),
        )
        assertEquals(
            TranscriptScrollAnchor(key = "latest", scrollOffset = 0),
            tracker.anchorFor(listOf("oldest", "latest")),
        )
    }

    private fun turn(
        key: String,
        response: String,
    ) = ConversationTurnUiModel(
        key = key,
        userPrompt = null,
        thinking = "",
        statusText = "",
        tools = emptyList(),
        response = response,
        status = ConversationTurnStatus.COMPLETE,
    )
}
