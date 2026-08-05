package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class ConversationTurnUiModelTest {
    @Test
    fun `canonical sections preserve Hermes order with stable identity and explicit lifecycle`() {
        fun metadata(
            key: String,
            status: HermesConversationSectionStatus = HermesConversationSectionStatus.COMPLETE,
        ) = HermesConversationSectionMetadata(
            key = key,
            status = status,
            revision = HermesConversationSectionRevision.Numbered(7),
        )

        val sections = CanonicalConversationSections.of(
            HermesConversationSection.UserPrompt(
                metadata = metadata("prompt:1"),
                prompt = ConversationPromptUiModel("message:1", "Ship it"),
            ),
            HermesConversationSection.Todo(metadata("todo:1"), emptyList()),
            HermesConversationSection.Thinking(metadata("thinking:1"), "Planning"),
            HermesConversationSection.ToolGroup(metadata("tools:1"), emptyList()),
            HermesConversationSection.Subagents(
                metadata("subagents:1", HermesConversationSectionStatus.RUNNING),
                subagents = emptyList(),
                moaReferences = emptyList(),
            ),
            HermesConversationSection.Activity(metadata("activity:1"), "Working"),
            HermesConversationSection.ResponseBoundary(metadata("boundary:1")),
            HermesConversationSection.AssistantResponse(metadata("response:1"), "Done"),
            HermesConversationSection.Event(metadata("event:1"), "model changed"),
            HermesConversationSection.Diff(metadata("diff:1"), "+done"),
            HermesConversationSection.Error(
                metadata("error:1", HermesConversationSectionStatus.ERROR),
                "failed",
            ),
            HermesConversationSection.TokenSummary(
                metadata("tokens:1"),
                HermesConversationTokenSummary(totalTokens = 42),
            ),
            HermesConversationSection.PendingInput(
                metadata("pending:1", HermesConversationSectionStatus.PENDING),
                HermesConversationPendingInput(
                    kind = HermesConversationPendingInputKind.CLARIFICATION,
                    prompt = "Which target?",
                ),
            ),
        )

        val turn = ConversationTurnUiModel(
            key = "turn:1",
            userPrompt = null,
            thinking = "",
            statusText = "",
            tools = emptyList(),
            response = "",
            status = ConversationTurnStatus.COMPLETE,
            sections = sections,
        )

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TODO,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.SUBAGENTS,
                HermesConversationSectionKind.ACTIVITY,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.EVENT,
                HermesConversationSectionKind.DIFF,
                HermesConversationSectionKind.ERROR,
                HermesConversationSectionKind.TOKEN_SUMMARY,
                HermesConversationSectionKind.PENDING_INPUT,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            sections.map { it.metadata.key },
            turn.sections.map { it.key },
        )
        assertEquals(
            HermesConversationSectionRevision.Numbered(7),
            turn.sections.first().revision,
        )
        assertFailsWith<IllegalArgumentException> {
            CanonicalConversationSections.of(
                HermesConversationSection.Event(metadata("duplicate"), "first"),
                HermesConversationSection.Event(metadata("duplicate"), "second"),
            )
        }
    }

    @Test
    fun `disclosure policy expands process detail but hides ordinary activity by default`() {
        val policy = ConversationDisclosurePolicy()

        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            policy.mode(ConversationDisclosureSection.TODO),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            policy.mode(ConversationDisclosureSection.THINKING),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            policy.mode(ConversationDisclosureSection.TOOLS),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            policy.mode(ConversationDisclosureSection.SUBAGENTS),
        )
        assertEquals(
            ConversationDisclosureMode.HIDDEN,
            policy.mode(ConversationDisclosureSection.ACTIVITY),
        )
    }

    @Test
    fun `explicit disclosure toggle is isolated by stable turn key and section`() {
        val policy = ConversationDisclosurePolicy()
        val toolsKey = ConversationDisclosureStateKey(
            turnKey = "stable-turn",
            section = ConversationDisclosureSection.TOOLS,
        )

        val collapsed = ConversationDisclosureState().toggled(toolsKey, policy)

        assertEquals(ConversationDisclosureMode.COLLAPSED, collapsed.mode(toolsKey, policy))
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            collapsed.mode(
                toolsKey.copy(section = ConversationDisclosureSection.THINKING),
                policy,
            ),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            collapsed.mode(toolsKey.copy(turnKey = "next-turn"), policy),
        )
    }

    @Test
    fun `explicit disclosure toggle remains when a section disappears and returns`() {
        val policy = ConversationDisclosurePolicy()
        val toolsKey = ConversationDisclosureStateKey(
            turnKey = "streaming-turn",
            section = ConversationDisclosureSection.TOOLS,
        )
        val collapsed = ConversationDisclosureState().toggled(toolsKey, policy)

        // No section lookup occurs while streaming temporarily removes the tools section.
        val afterSectionReturns = collapsed

        assertEquals(
            ConversationDisclosureMode.COLLAPSED,
            afterSectionReturns.mode(toolsKey, policy),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            afterSectionReturns.toggled(toolsKey, policy).mode(toolsKey, policy),
        )
    }

    @Test
    fun `session disclosure registry survives history changes and isolates sessions`() {
        val policy = ConversationDisclosurePolicy()
        val toolsKey = ConversationDisclosureStateKey(
            turnKey = "stable-turn",
            section = ConversationDisclosureSection.TOOLS,
        )

        val collapsed = ConversationDisclosureRegistry()
            .toggled(
                sessionKey = "session-a",
                key = toolsKey,
                policy = policy,
            )

        assertEquals(
            ConversationDisclosureMode.COLLAPSED,
            collapsed.state("session-a").mode(toolsKey, policy),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            collapsed.state("session-b").mode(toolsKey, policy),
        )
        assertEquals(
            ConversationDisclosureMode.COLLAPSED,
            collapsed.state("session-a").mode(toolsKey, policy),
        )
    }

    @Test
    fun `long running locator expands its target and collapses its sibling in place`() {
        val policy = ConversationDisclosurePolicy()
        val todoKey = ConversationDisclosureStateKey(
            turnKey = "active-turn",
            section = ConversationDisclosureSection.TODO,
        )
        val subagentKey = todoKey.copy(section = ConversationDisclosureSection.SUBAGENTS)

        val focused = ConversationDisclosureRegistry().focused(
            sessionKey = "session-a",
            key = subagentKey,
        )

        assertEquals(
            ConversationDisclosureMode.COLLAPSED,
            focused.state("session-a").mode(todoKey, policy),
        )
        assertEquals(
            ConversationDisclosureMode.EXPANDED,
            focused.state("session-a").mode(subagentKey, policy),
        )
    }

    @Test
    fun `response separator requires started response and visible process`() {
        val turn = ConversationTurnUiModel(
            key = "turn-separator",
            userPrompt = null,
            thinking = "Inspecting",
            statusText = "",
            tools = emptyList(),
            response = "",
            status = ConversationTurnStatus.ERROR,
            error = "Interrupted",
        )

        assertEquals(false, shouldShowResponseSeparator(turn, processVisible = true))
        assertEquals(false, shouldShowResponseSeparator(turn.copy(response = "Answer"), processVisible = false))
        assertEquals(
            true,
            shouldShowResponseSeparator(
                turn.copy(response = "Partial", status = ConversationTurnStatus.STREAMING),
                processVisible = true,
            ),
        )
        assertEquals(
            true,
            shouldShowResponseSeparator(
                turn.copy(response = "Answer", status = ConversationTurnStatus.COMPLETE),
                processVisible = true,
            ),
        )
    }
}
