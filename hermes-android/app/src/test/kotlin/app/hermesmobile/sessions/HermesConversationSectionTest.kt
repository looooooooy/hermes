package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals

class HermesConversationSectionTest {
    @Test
    fun `subagent and moa process metadata preserve provided progress and token summary`() {
        val tokenSummary = HermesConversationTokenSummary(
            inputTokens = 100,
            outputTokens = 25,
            reasoningTokens = 10,
            totalTokens = 135,
        )
        val subagent = HermesConversationSubagent(
            key = "child-1",
            goal = "Inspect the projector",
            status = HermesConversationSectionStatus.COMPLETE,
            durationSeconds = 1.5,
            taskIndex = 0,
            taskCount = 2,
            tokenSummary = tokenSummary,
            apiCalls = 3,
        )
        val progress = HermesConversationMoaProgress(
            phase = "aggregator",
            aggregator = "test-model",
            refsDone = 2,
            refsTotal = 3,
        )
        val section = HermesConversationSection.Subagents(
            metadata = HermesConversationSectionMetadata(
                key = "turn-1:subagents",
                status = HermesConversationSectionStatus.COMPLETE,
            ),
            subagents = listOf(subagent),
            moaReferences = emptyList(),
            moaProgress = progress,
        )

        assertEquals(1.5, section.subagents.single().durationSeconds)
        assertEquals(tokenSummary, section.subagents.single().tokenSummary)
        assertEquals(progress, section.moaProgress)
    }
}
