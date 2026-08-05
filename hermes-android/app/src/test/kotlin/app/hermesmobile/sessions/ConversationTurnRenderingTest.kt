package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals

class ConversationTurnRenderingTest {
    @Test
    fun `canonical sections render in caller order with stable keys`() {
        val sections = CanonicalConversationSections.of(
            HermesConversationSection.UserPrompt(
                metadata = metadata("turn-1:prompt"),
                prompt = ConversationPromptUiModel("prompt-1", "Inspect the workspace"),
            ),
            HermesConversationSection.Thinking(
                metadata = metadata("turn-1:thinking", HermesConversationSectionStatus.STREAMING),
                text = "Inspecting",
            ),
            HermesConversationSection.ResponseBoundary(
                metadata = metadata("turn-1:response-boundary"),
            ),
            HermesConversationSection.AssistantResponse(
                metadata = metadata("turn-1:response", HermesConversationSectionStatus.STREAMING),
                text = "Partial answer",
            ),
        )
        val turn = ConversationTurnUiModel(
            key = "turn-1",
            userPrompt = null,
            thinking = "legacy thinking must not replace canonical content",
            statusText = "",
            tools = emptyList(),
            response = "legacy response must not replace canonical content",
            status = ConversationTurnStatus.STREAMING,
            sections = sections,
        )

        val rendered = turn.renderSections()

        assertEquals(
            listOf(
                "turn-1:prompt",
                "turn-1:thinking",
                "turn-1:response-boundary",
                "turn-1:response",
            ),
            rendered.map(HermesConversationSection::key),
        )
        assertEquals(sections.toList(), rendered)
    }

    @Test
    fun `section render identity ignores content revision`() {
        fun response(revision: Long) = HermesConversationSection.AssistantResponse(
            metadata = HermesConversationSectionMetadata(
                key = "turn-1:response",
                status = HermesConversationSectionStatus.STREAMING,
                revision = HermesConversationSectionRevision.Numbered(revision),
            ),
            text = "revision $revision",
        )

        assertEquals(
            response(1).renderIdentityKey(),
            response(2).renderIdentityKey(),
        )
    }

    @Test
    fun `streaming legacy turn exposes response boundary when response starts`() {
        val turn = ConversationTurnUiModel(
            key = "turn-legacy",
            userPrompt = ConversationPromptUiModel("prompt-legacy", "Inspect legacy output"),
            thinking = "Inspecting",
            statusText = "Working",
            tools = listOf(
                ConversationToolUiModel(
                    key = "tool-1",
                    toolId = "call-1",
                    name = "terminal",
                    status = ConversationToolStatus.RUNNING,
                ),
            ),
            response = "Partial answer",
            status = ConversationTurnStatus.STREAMING,
        )

        val rendered = turn.renderSections()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.ACTIVITY,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            rendered.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf(
                "turn-legacy:user-prompt",
                "turn-legacy:thinking",
                "turn-legacy:tools",
                "turn-legacy:activity",
                "turn-legacy:response-boundary",
                "turn-legacy:response",
            ),
            rendered.map(HermesConversationSection::key),
        )
    }

    @Test
    fun `completed legacy turn exposes one response boundary after process work`() {
        val turn = ConversationTurnUiModel(
            key = "turn-complete",
            userPrompt = null,
            thinking = "Inspected",
            statusText = "",
            tools = emptyList(),
            response = "Final answer",
            status = ConversationTurnStatus.COMPLETE,
        )

        assertEquals(
            listOf(
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.renderSections().map(HermesConversationSection::kind),
        )
    }

    private fun metadata(
        key: String,
        status: HermesConversationSectionStatus = HermesConversationSectionStatus.COMPLETE,
    ) = HermesConversationSectionMetadata(key = key, status = status)
}
