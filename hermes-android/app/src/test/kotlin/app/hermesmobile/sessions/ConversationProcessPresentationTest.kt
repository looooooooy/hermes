package app.hermesmobile.sessions

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class ConversationProcessPresentationTest {
    @Test
    fun `process sections and items keep typed stable keys across streaming updates`() {
        val initial = turn(
            thinking = "Inspecting",
            statusText = "Working",
            tools = listOf(tool(key = "tool:call-1")),
        )

        val before = ConversationProcessPresentationProjector.project(initial)
        val after = ConversationProcessPresentationProjector.project(
            initial.copy(
                thinking = "Inspecting more files",
                statusText = "Still working",
                tools = initial.tools + tool(key = "tool:call-2", toolId = "call-2"),
            ),
        )

        assertEquals(
            listOf(
                ConversationDisclosureSection.THINKING,
                ConversationDisclosureSection.TOOLS,
                ConversationDisclosureSection.ACTIVITY,
            ),
            before.sections.map(ConversationProcessSectionPresentation::section),
        )
        assertEquals(before.sections.map { it.key }, after.sections.map { it.key })
        assertEquals(
            before.section(ConversationDisclosureSection.THINKING)?.items?.single()?.key,
            after.section(ConversationDisclosureSection.THINKING)?.items?.single()?.key,
        )
        val beforeTool = assertIs<ConversationProcessItemPresentation.Tool>(
            before.section(ConversationDisclosureSection.TOOLS)?.items?.single(),
        )
        val afterTool = assertIs<ConversationProcessItemPresentation.Tool>(
            after.section(ConversationDisclosureSection.TOOLS)?.items?.first(),
        )
        assertEquals(beforeTool.key, afterTool.key)
        assertEquals("tool:call-1", beforeTool.key.sourceKey)
        assertEquals(
            before.section(ConversationDisclosureSection.ACTIVITY)?.items?.single()?.key,
            after.section(ConversationDisclosureSection.ACTIVITY)?.items?.single()?.key,
        )
    }

    @Test
    fun `tool detail keys are scoped to stable tool identity`() {
        val first = tool(key = "tool:call-1").copy(
            arguments = """{"command":"pwd"}""",
            output = "first output",
            status = ConversationToolStatus.COMPLETE,
        )
        val second = tool(key = "tool:call-2", toolId = "call-2").copy(
            arguments = """{"command":"date"}""",
            output = "second output",
            status = ConversationToolStatus.COMPLETE,
        )

        val tools = ConversationProcessPresentationProjector.project(
            turn(tools = listOf(first, second)),
        ).section(ConversationDisclosureSection.TOOLS)?.items.orEmpty()
            .map { item -> assertIs<ConversationProcessItemPresentation.Tool>(item) }

        assertEquals(
            listOf(
                ConversationProcessToolDetailKind.ARGUMENTS,
                ConversationProcessToolDetailKind.RESULT,
            ),
            tools[0].details.map(ConversationProcessToolDetailPresentation::kind),
        )
        assertEquals(
            listOf(
                ConversationProcessToolDetailKind.ARGUMENTS,
                ConversationProcessToolDetailKind.RESULT,
            ),
            tools[1].details.map(ConversationProcessToolDetailPresentation::kind),
        )
        assertEquals(tools[0].key, tools[0].details.single { it.kind == ConversationProcessToolDetailKind.RESULT }.key.toolKey)
        assertEquals(tools[1].key, tools[1].details.single { it.kind == ConversationProcessToolDetailKind.RESULT }.key.toolKey)
        assertEquals(false, tools[0].details.map { it.key }.intersect(tools[1].details.map { it.key }.toSet()).isNotEmpty())
    }

    @Test
    fun `failed tool without payload projects a visible error fallback`() {
        val failedTool = tool(key = "tool:failed").copy(
            callLabel = "Terminal(\"restricted\")",
            status = ConversationToolStatus.ERROR,
            error = "",
        )

        val presentation = ConversationProcessPresentationProjector.project(
            turn(tools = listOf(failedTool)),
        )
        val projectedTool = assertIs<ConversationProcessItemPresentation.Tool>(
            presentation.section(ConversationDisclosureSection.TOOLS)?.items?.single(),
        )
        val errorDetail = projectedTool.details.single()

        assertEquals(ConversationProcessToolDetailKind.ERROR, errorDetail.kind)
        assertEquals("No error details were provided.", errorDetail.body)
        assertEquals(projectedTool.key, presentation.visibleErrorFallback?.key)
        assertEquals("No error details were provided.", presentation.visibleErrorFallback?.text)
    }

    @Test
    fun `tool header keeps one call line and exposes lifecycle as visible text`() {
        val presentation = presentHermesToolHeader(
            label = "ReadFile(\"/workspace/a/very/long/path/that/must/not/wrap/File.kt\")",
            lifecycleDescription = "Complete",
        )

        assertEquals(1, presentation.maxLines)
        assertEquals(
            listOf(
                "ReadFile(\"/workspace/a/very/long/path/that/must/not/wrap/File.kt\")",
                "Complete",
            ),
            presentation.visibleTexts,
        )
        assertEquals("Complete", presentation.stateDescription)
    }

    @Test
    fun `thinking presentation preserves markdown and streaming lifecycle`() {
        val markdown = "## Plan\n\nUse **cached blocks**."
        val presentation = presentHermesThinking(
            HermesConversationSection.Thinking(
                metadata = HermesConversationSectionMetadata(
                    key = "turn:thinking",
                    status = HermesConversationSectionStatus.STREAMING,
                ),
                text = markdown,
            ),
        )

        assertEquals(markdown, presentation.markdown)
        assertEquals(true, presentation.streaming)
    }

    private fun turn(
        thinking: String = "",
        statusText: String = "",
        tools: List<ConversationToolUiModel> = emptyList(),
    ) = ConversationTurnUiModel(
        key = "turn:stable",
        userPrompt = null,
        thinking = thinking,
        statusText = statusText,
        tools = tools,
        response = "",
        status = ConversationTurnStatus.STREAMING,
    )

    private fun tool(
        key: String,
        toolId: String = "call-1",
    ) = ConversationToolUiModel(
        key = key,
        toolId = toolId,
        name = "terminal",
        callLabel = "Terminal(\"pwd\")",
        status = ConversationToolStatus.RUNNING,
    )
}
