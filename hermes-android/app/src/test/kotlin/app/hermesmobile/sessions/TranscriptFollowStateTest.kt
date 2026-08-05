package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue

class TranscriptFollowStateTest {
    @Test
    fun `content changes stay followed while viewport remains at latest`() {
        val state = TranscriptFollowState()
            .onContentChanged()
            .onContentChanged()

        assertTrue(state.isFollowingLatest)
        assertEquals(0, state.unseenUpdates)
        assertTrue(state.shouldScrollToLatest)
    }

    @Test
    fun `backward user scroll pauses follow before another delta arrives`() {
        val paused = TranscriptFollowState()
            .onViewportChanged(
                isScrollInProgress = true,
                lastScrolledBackward = true,
                isAtLatest = false,
            )
            .onContentChanged()

        assertFalse(paused.isFollowingLatest)
        assertEquals(1, paused.unseenUpdates)
        assertFalse(paused.shouldScrollToLatest)
    }

    @Test
    fun `several streaming updates collapse into a bounded unseen counter`() {
        val paused = TranscriptFollowState(isFollowingLatest = false)
            .onContentChanged(updateCount = 3)
            .onContentChanged(updateCount = 2)

        assertEquals(5, paused.unseenUpdates)
        assertFalse(paused.shouldScrollToLatest)
    }

    @Test
    fun `unseen updates saturate at the presentation bound`() {
        val paused = TranscriptFollowState(
            isFollowingLatest = false,
            unseenUpdates = MAX_TRANSCRIPT_UNSEEN_UPDATES - 1,
        ).onContentChanged(updateCount = 10)

        assertEquals(MAX_TRANSCRIPT_UNSEEN_UPDATES, paused.unseenUpdates)
    }

    @Test
    fun `reaching latest or requesting jump resumes follow and clears unseen updates`() {
        val paused = TranscriptFollowState(
            isFollowingLatest = false,
            unseenUpdates = 4,
        )

        val reachedLatest = paused.onViewportChanged(
            isScrollInProgress = false,
            lastScrolledBackward = false,
            isAtLatest = true,
        )
        assertTrue(reachedLatest.isFollowingLatest)
        assertEquals(0, reachedLatest.unseenUpdates)

        val jumped = paused.onJumpToLatest()
        assertTrue(jumped.isFollowingLatest)
        assertEquals(0, jumped.unseenUpdates)
        assertTrue(jumped.shouldScrollToLatest)
    }

    @Test
    fun `new session resets follow policy`() {
        val reset = TranscriptFollowState(
            isFollowingLatest = false,
            unseenUpdates = 9,
        ).onSessionChanged()

        assertEquals(TranscriptFollowState(), reset)
    }

    @Test
    fun `frame coalescer emits at most one follow request`() {
        val coalescer = TranscriptFollowRequestCoalescer()

        repeat(3) { coalescer.request() }

        assertTrue(coalescer.consumeFrame(isViewportAtLatest = false))
        assertFalse(coalescer.consumeFrame(isViewportAtLatest = false))
    }

    @Test
    fun `frame coalescer consumes a redundant request without scrolling`() {
        val coalescer = TranscriptFollowRequestCoalescer()
        coalescer.request()

        assertFalse(coalescer.consumeFrame(isViewportAtLatest = true))
        assertFalse(coalescer.consumeFrame(isViewportAtLatest = false))
    }

    @Test
    fun `backward user scroll wins race with a pending follow frame`() {
        val coalescer = TranscriptFollowRequestCoalescer()
        coalescer.request()

        assertFalse(
            coalescer.consumeFrame(
                isViewportAtLatest = false,
                isUserScrollingBackward = true,
            ),
        )
        assertFalse(coalescer.consumeFrame(isViewportAtLatest = false))
    }

    @Test
    fun `tall final item is latest only when its end reaches the viewport end`() {
        assertFalse(
            isTranscriptViewportAtLatest(
                totalItems = 3,
                lastVisibleIndex = 2,
                lastVisibleEndOffset = 1_400,
                viewportEndOffset = 800,
            ),
        )
        assertTrue(
            isTranscriptViewportAtLatest(
                totalItems = 3,
                lastVisibleIndex = 2,
                lastVisibleEndOffset = 800,
                viewportEndOffset = 800,
            ),
        )
    }

    @Test
    fun `history prepend does not look like a new latest update`() {
        val older = turn(key = "older", response = "Older history")
        val latest = turn(key = "latest", response = "Current answer")

        val beforePrepend = listOf(latest).latestContentRevision()
        val afterPrepend = listOf(older, latest).latestContentRevision()

        assertEquals(beforePrepend, afterPrepend)
        assertNotEquals(
            beforePrepend,
            listOf(latest.copy(response = "Current answer with delta")).latestContentRevision(),
        )
        assertNotEquals(
            beforePrepend,
            listOf(latest, turn(key = "next", response = "Next answer")).latestContentRevision(),
        )
    }

    @Test
    fun `realtime event cursor advances latest revision without changing turn identity`() {
        val latest = turn(key = "latest", response = "Streaming answer")

        val firstDelta = listOf(latest).latestContentRevision(
            connectionEpoch = 7,
            eventOrdinal = 41,
        )
        val nextDelta = listOf(latest).latestContentRevision(
            connectionEpoch = 7,
            eventOrdinal = 42,
        )

        assertNotEquals(firstDelta, nextDelta)
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
