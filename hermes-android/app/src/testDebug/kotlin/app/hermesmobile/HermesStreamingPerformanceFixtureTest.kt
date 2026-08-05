package app.hermesmobile

import app.hermesmobile.sessions.ConversationTurnProjector
import app.hermesmobile.sessions.SessionTimelineItem
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class HermesStreamingPerformanceFixtureTest {
    @Test
    fun `streaming performance fixture uses authoritative history reducer timeline and production projector`() {
        val state = streamingPerformanceReviewState()
        val transcript = assertNotNull(state.transcript)
        val realtime = assertNotNull(state.realtime)

        assertEquals(192, transcript.messages.size)
        assertTrue(realtime.timeline.any { item ->
            item is SessionTimelineItem.AssistantTurn && item.thinking.contains("streaming Markdown")
        })
        assertTrue(realtime.timeline.any { item ->
            item is SessionTimelineItem.AssistantTurn && item.text.contains("production renderer")
        })
        assertTrue(realtime.timeline.any { item ->
            item is SessionTimelineItem.ToolActivity && item.output.contains("synthetic tool line")
        })
        assertTrue(realtime.timeline.any { item ->
            item is SessionTimelineItem.AssistantTurn && item.status.name == "COMPLETE"
        })

        val projected = ConversationTurnProjector().project(transcript, realtime)
        assertTrue(projected.size > 96)
        assertTrue(projected.any { turn -> turn.thinking.contains("streaming Markdown") })
        assertTrue(projected.any { turn -> turn.response.contains("production renderer") })
        assertTrue(projected.any { turn -> turn.tools.any { tool -> tool.output.contains("synthetic tool line") } })
    }
}
