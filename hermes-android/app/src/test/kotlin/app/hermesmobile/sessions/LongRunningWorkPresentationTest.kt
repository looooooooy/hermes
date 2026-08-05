package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull

class LongRunningWorkPresentationTest {
    @Test
    fun `running turn projects todo and subagent locators from authoritative sections`() {
        val turn = ConversationTurnUiModel(
            key = "turn-active",
            userPrompt = null,
            thinking = "",
            statusText = "",
            tools = emptyList(),
            response = "",
            status = ConversationTurnStatus.STREAMING,
            sections = CanonicalConversationSections.of(
                HermesConversationSection.Todo(
                    metadata = metadata("turn-active:todo"),
                    items = listOf(
                        todo("lock", HermesConversationTodoStatus.COMPLETED),
                        todo("composer", HermesConversationTodoStatus.COMPLETED),
                        todo("review", HermesConversationTodoStatus.IN_PROGRESS),
                        todo("acceptance", HermesConversationTodoStatus.PENDING),
                    ),
                ),
                HermesConversationSection.Subagents(
                    metadata = metadata("turn-active:subagents"),
                    subagents = listOf(
                        HermesConversationSubagent(
                            key = "composer-review",
                            goal = "Review composer hierarchy",
                            status = HermesConversationSectionStatus.RUNNING,
                        ),
                        HermesConversationSubagent(
                            key = "acceptance-review",
                            goal = "Review Android acceptance",
                            status = HermesConversationSectionStatus.PENDING,
                        ),
                    ),
                    moaReferences = emptyList(),
                ),
            ),
        )

        val presentation = assertNotNull(
            longRunningWorkPresentation(running = true, turns = listOf(turn)),
        )

        assertEquals(
            listOf(LongRunningWorkKind.TODO, LongRunningWorkKind.SUBAGENT),
            presentation.items.map(LongRunningWorkItemPresentation::kind),
        )
        assertEquals(
            LongRunningWorkItemPresentation(
                turnKey = "turn-active",
                sectionKey = ConversationDisclosureStateKey(
                    turnKey = "turn-active",
                    section = ConversationDisclosureSection.TODO,
                ),
                kind = LongRunningWorkKind.TODO,
                progressNumerator = 2,
                progressDenominator = 4,
                currentLabel = "review",
                status = HermesTranscriptStatus.Running,
            ),
            presentation.items[0],
        )
        assertEquals(
            LongRunningWorkItemPresentation(
                turnKey = "turn-active",
                sectionKey = ConversationDisclosureStateKey(
                    turnKey = "turn-active",
                    section = ConversationDisclosureSection.SUBAGENTS,
                ),
                kind = LongRunningWorkKind.SUBAGENT,
                progressNumerator = 1,
                progressDenominator = 2,
                currentLabel = "Review composer hierarchy",
                status = HermesTranscriptStatus.Running,
            ),
            presentation.items[1],
        )
    }

    @Test
    fun `long running dock hides outside a running turn or without active work`() {
        val completeTurn = ConversationTurnUiModel(
            key = "turn-complete",
            userPrompt = null,
            thinking = "",
            statusText = "",
            tools = emptyList(),
            response = "Done",
            status = ConversationTurnStatus.COMPLETE,
            sections = CanonicalConversationSections.of(
                HermesConversationSection.Todo(
                    metadata = HermesConversationSectionMetadata(
                        key = "turn-complete:todo",
                        status = HermesConversationSectionStatus.COMPLETE,
                    ),
                    items = listOf(todo("done", HermesConversationTodoStatus.COMPLETED)),
                ),
            ),
        )

        assertNull(longRunningWorkPresentation(running = false, turns = listOf(completeTurn)))
        assertNull(longRunningWorkPresentation(running = true, turns = listOf(completeTurn)))
    }

    private fun metadata(key: String) = HermesConversationSectionMetadata(
        key = key,
        status = HermesConversationSectionStatus.RUNNING,
    )

    private fun todo(
        content: String,
        status: HermesConversationTodoStatus,
    ) = HermesConversationTodoItem(
        key = content,
        content = content,
        status = status,
    )
}
