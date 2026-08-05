package app.hermesmobile.sessions

import app.hermesmobile.protocol.sessions.SessionMessageProjection
import kotlin.test.Test
import kotlin.test.assertNotEquals

class SessionBrowserMessageKeyTest {
    @Test
    fun `history rows sharing a message id still have unique compose keys`() {
        val toolCall = message(messageId = 1L, role = "assistant")
        val toolResult = message(messageId = 1L, role = "tool")

        assertNotEquals(
            historicalMessageKey(index = 0, message = toolCall),
            historicalMessageKey(index = 1, message = toolResult),
        )
    }

    private fun message(
        messageId: Long,
        role: String,
    ) = SessionMessageProjection(
        messageId = messageId,
        role = role,
        content = null,
        timestampEpochSeconds = null,
        reasoning = null,
        reasoningContent = null,
        reasoningDetails = null,
        toolCallId = "tool-1",
        toolCalls = null,
        toolName = null,
        displayKind = null,
        displayMetadata = null,
    )
}
