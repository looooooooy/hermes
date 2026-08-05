package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.GatewayEvent
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class ConversationTurnProjectorTest {
    private val projector = ConversationTurnProjector()

    @Test
    fun `history groups a user tool activity and final answer into one turn`() {
        val transcript = transcript(
            message(
                id = 1,
                role = "user",
                content = "Inspect the workspace",
            ),
            message(
                id = 2,
                role = "assistant",
                content = "",
                reasoning = "I should inspect the current directory.",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-1")
                            put("type", "function")
                            put(
                                "function",
                                buildJsonObject {
                                    put("name", "terminal")
                                    put("arguments", "{\"command\":\"pwd\"}")
                                },
                            )
                        },
                    )
                },
            ),
            message(
                id = 3,
                role = "tool",
                content = "/workspace\n",
                toolCallId = "call-1",
                toolName = "terminal",
            ),
            message(
                id = 4,
                role = "assistant",
                content = "The workspace is `/workspace`.",
            ),
        )

        val turns = projector.project(transcript = transcript, realtime = null)

        assertEquals(1, turns.size)
        val turn = turns.single()
        assertEquals("turn:response:4", turn.key)
        assertEquals("Inspect the workspace", turn.userPrompt?.text)
        assertEquals("I should inspect the current directory.", turn.thinking)
        assertEquals("The workspace is `/workspace`.", turn.response)
        assertEquals(ConversationTurnStatus.COMPLETE, turn.status)
        assertEquals(1, turn.tools.size)
        assertEquals("call-1", turn.tools.single().toolId)
        assertEquals("terminal", turn.tools.single().name)
        assertEquals("Command: pwd", turn.tools.single().arguments)
        assertEquals(
            listOf(ConversationToolDetailUiModel("Command", "pwd")),
            turn.tools.single().argumentDetails,
        )
        assertEquals("/workspace\n", turn.tools.single().output)
        assertEquals(ConversationToolStatus.COMPLETE, turn.tools.single().status)
        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
    }

    @Test
    fun `historical and realtime assistant markdown is sanitized without an eight thousand character cliff`() {
        val historicalText = "history\n" + "h".repeat(9_000) + "\ntoken=history-secret\nend"
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect"),
            message(id = 2, role = "assistant", content = historicalText),
        )
        val historical = projector.project(transcript, realtime = null).single()

        assertTrue(historical.response.length > 8_000)
        assertTrue(historical.response.endsWith("token=[redacted]\nend"))
        assertFalse(historical.response.contains("history-secret"))

        val liveText = "live\n" + "l".repeat(9_000) + "\npassword=live-secret\nend"
        val realtime = RealtimeSessionReducer().seed(
            transcript = transcript(
                message(id = 1, role = "user", content = "Inspect"),
            ),
            runtimeSessionId = RuntimeSessionId("runtime-safe-markdown"),
            connectionEpoch = 1,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:1:1",
                    text = liveText,
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )
        val live = projector.project(realtime.transcript, realtime).single()

        assertTrue(live.response.length > 8_000)
        assertTrue(live.response.endsWith("password=[redacted]\nend"))
        assertFalse(live.response.contains("live-secret"))
    }

    @Test
    fun `history projects object tool payloads as Hermes readable details`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect the workspace"),
            message(
                id = 2,
                role = "assistant",
                content = "",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-1")
                            put(
                                "function",
                                buildJsonObject {
                                    put("name", "terminal")
                                    put(
                                        "arguments",
                                        """{"command":"pwd","workdir":"/workspace"}""",
                                    )
                                },
                            )
                        },
                    )
                },
            ),
            SessionMessageProjection(
                messageId = 3,
                role = "tool",
                content = buildJsonObject {
                    put("output", "/workspace\n")
                    put("exit_code", 0)
                },
                timestampEpochSeconds = 3.0,
                reasoning = null,
                reasoningContent = null,
                reasoningDetails = null,
                toolCallId = "call-1",
                toolCalls = null,
                toolName = "terminal",
                displayKind = null,
                displayMetadata = null,
            ),
            message(id = 4, role = "assistant", content = "The workspace is ready."),
        )

        val tool = projector.project(transcript, realtime = null).single().tools.single()

        assertEquals("Terminal(\"pwd\")", tool.callLabel)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Command", "pwd"),
                ConversationToolDetailUiModel("Workdir", "/workspace"),
            ),
            tool.argumentDetails,
        )
        assertEquals("/workspace\n", tool.output)
        assertEquals(
            listOf(ConversationToolDetailUiModel("Exit code", "0")),
            tool.resultDetails,
        )
    }

    @Test
    fun `history preserves JsonObject tool arguments until readable presentation`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect"),
            message(
                id = 2,
                role = "assistant",
                content = "",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-object")
                            put(
                                "function",
                                buildJsonObject {
                                    put("name", "terminal")
                                    put(
                                        "arguments",
                                        buildJsonObject {
                                            put("command", "pwd")
                                            put("workdir", "/workspace")
                                        },
                                    )
                                },
                            )
                        },
                    )
                },
            ),
        )

        val tool = projector.project(transcript, realtime = null).single().tools.single()

        assertEquals("Terminal(\"pwd\")", tool.callLabel)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Command", "pwd"),
                ConversationToolDetailUiModel("Workdir", "/workspace"),
            ),
            tool.argumentDetails,
        )
    }

    @Test
    fun `history skips blank nested arguments and uses presentable top level arguments`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect"),
            message(
                id = 2,
                role = "assistant",
                content = "",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-fallback")
                            put(
                                "function",
                                buildJsonObject {
                                    put("name", "terminal")
                                    put("arguments", JsonNull)
                                },
                            )
                            put(
                                "arguments",
                                buildJsonObject {
                                    put("command", "pwd")
                                    put("workdir", "/workspace")
                                },
                            )
                        },
                    )
                },
            ),
        )

        val tool = projector.project(transcript, realtime = null).single().tools.single()

        assertEquals("Terminal(\"pwd\")", tool.callLabel)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Command", "pwd"),
                ConversationToolDetailUiModel("Workdir", "/workspace"),
            ),
            tool.argumentDetails,
        )
    }

    @Test
    fun `assistant content is preserved when the same message also starts tools`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect"),
            message(
                id = 2,
                role = "assistant",
                content = "I will inspect the workspace first.",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-1")
                            put("name", "terminal")
                            put("arguments", "{\"command\":\"pwd\"}")
                        },
                    )
                },
            ),
        )

        val turn = projector.project(transcript, realtime = null).single()

        assertEquals("I will inspect the workspace first.", turn.response)
        assertEquals(1, turn.tools.size)
        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TOOL_GROUP,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
    }

    @Test
    fun `historical tool without a terminal result stays explicitly unknown`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect"),
            message(
                id = 2,
                role = "assistant",
                content = "I found a partial answer before interruption.",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-interrupted")
                            put("name", "terminal")
                            put("arguments", "{\"command\":\"pwd\"}")
                        },
                    )
                },
            ),
        )

        val turn = projector.project(transcript, realtime = null).single()

        assertEquals("I found a partial answer before interruption.", turn.response)
        assertEquals("UNKNOWN", turn.tools.single().status.name)
        assertEquals(ConversationTurnStatus.INCOMPLETE, turn.status)
        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TOOL_GROUP,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
    }

    @Test
    fun `assistant content survives malformed tool calls that cannot be projected`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect"),
            message(
                id = 2,
                role = "assistant",
                content = "The tool metadata was malformed, but this text is valid.",
                toolCalls = buildJsonArray { add(JsonPrimitive("not-an-object")) },
            ),
        )

        val turn = projector.project(transcript, realtime = null).single()

        assertEquals("The tool metadata was malformed, but this text is valid.", turn.response)
        assertEquals(emptyList(), turn.tools)
    }

    @Test
    fun `historical baseline produces successive live updates without mutation leakage`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Inspect the workspace"),
        )
        val baseline = projector.projectBaseline(transcript)
        val realtime = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-baseline"),
            connectionEpoch = 6,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:6:1",
                    text = "Partial answer",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )

        val first = projector.project(baseline, realtime).single()
        val second = projector.project(
            baseline,
            realtime.copy(
                timeline = listOf(
                    SessionTimelineItem.AssistantTurn(
                        key = "assistant:6:1",
                        text = "Updated answer",
                        status = AssistantTurnStatus.STREAMING,
                    ),
                ),
            ),
        ).single()

        assertEquals("Partial answer", first.response)
        assertEquals("Updated answer", second.response)
        assertEquals("Partial answer", first.response)
        assertEquals("", baseline.historicalTurns.single().response)
    }

    @Test
    fun `settled historical turns keep stable instances while active tail changes`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Earlier question"),
            message(id = 2, role = "assistant", content = "Earlier answer"),
            message(id = 3, role = "user", content = "Current question"),
        )
        val baseline = projector.projectBaseline(transcript)
        val realtime = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-stable-history"),
            connectionEpoch = 9,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:9:1",
                    text = "Partial current answer",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )

        val first = projector.project(baseline, realtime)
        val second = projector.project(
            baseline,
            realtime.copy(
                timeline = listOf(
                    SessionTimelineItem.AssistantTurn(
                        key = "assistant:9:1",
                        text = "Updated current answer",
                        status = AssistantTurnStatus.STREAMING,
                    ),
                ),
            ),
        )

        assertTrue(first.first() === baseline.historicalTurns.first())
        assertTrue(second.first() === baseline.historicalTurns.first())
        assertEquals("turn:response:2", first.first().key)
        assertEquals(first.first(), second.first())
        assertEquals("turn:message:3", first.last().key)
        assertEquals(first.last().key, second.last().key)
        assertEquals("Partial current answer", first.last().response)
        assertEquals("Updated current answer", second.last().response)
        assertFalse(first.last() === second.last())
    }

    @Test
    fun `new baseline after resync never reuses stale historical content`() {
        val initialTranscript = transcript(
            message(id = 1, role = "user", content = "Question before resync"),
            message(id = 2, role = "assistant", content = "Answer before resync"),
        )
        val refreshedTranscript = transcript(
            message(id = 1, role = "user", content = "Question after resync"),
            message(id = 2, role = "assistant", content = "Answer after resync"),
        )
        val initialBaseline = projector.projectBaseline(initialTranscript)
        val refreshedBaseline = projector.projectBaseline(refreshedTranscript)

        val initial = projector.project(initialBaseline, realtime = null).single()
        val refreshed = projector.project(refreshedBaseline, realtime = null).single()

        assertEquals("Question before resync", initial.userPrompt?.text)
        assertEquals("Answer before resync", initial.response)
        assertEquals("Question after resync", refreshed.userPrompt?.text)
        assertEquals("Answer after resync", refreshed.response)
        assertEquals(initial.key, refreshed.key)
        assertFalse(initial === refreshed)
        assertFalse(
            initialBaseline.historicalTurns.single() === refreshedBaseline.historicalTurns.single(),
        )
    }

    @Test
    fun `realtime output preserves chronological thinking tool and text segments`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Inspect the workspace"),
        )
        val runtimeId = RuntimeSessionId("runtime-chronological")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 15)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "reasoning.delta",
                runtimeId,
                buildJsonObject { put("text", "I need to inspect first.") },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "I’ll inspect the workspace first.") },
            ),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "call-chronological")
                    put("name", "terminal")
                    put("args_text", "pwd")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "call-chronological")
                    put("name", "terminal")
                    put("output", "/workspace")
                },
            ),
            GatewayEvent(
                "reasoning.delta",
                runtimeId,
                buildJsonObject { put("text", "The tool result confirms the path.") },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "The workspace is `/workspace`.") },
            ),
            GatewayEvent(
                "message.complete",
                runtimeId,
                buildJsonObject {
                    put(
                        "text",
                        "I’ll inspect the workspace first.The workspace is `/workspace`.",
                    )
                },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(15, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf(
                "I need to inspect first.",
                "The tool result confirms the path.",
            ),
            turn.sections.filterIsInstance<HermesConversationSection.Thinking>().map { it.text },
        )
        assertEquals(
            listOf(
                "I’ll inspect the workspace first.",
                "The workspace is `/workspace`.",
            ),
            turn.sections.filterIsInstance<HermesConversationSection.AssistantResponse>().map { it.text },
        )
    }

    @Test
    fun `assistant text between tools keeps separate chronological tool groups`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Inspect two sources"),
        )
        val runtimeId = RuntimeSessionId("runtime-separated-tools")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 16)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-a")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-a")
                    put("name", "terminal")
                    put("output", "A")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "The first source is ready.") },
            ),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-b")
                    put("name", "browser")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-b")
                    put("name", "browser")
                    put("output", "B")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Both sources are ready.") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(16, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()
        val toolGroups = turn.sections.filterIsInstance<HermesConversationSection.ToolGroup>()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(listOf("tool-a"), toolGroups[0].tools.map { it.toolId })
        assertEquals(listOf("tool-b"), toolGroups[1].tools.map { it.toolId })
    }

    @Test
    fun `history and realtime project canonical sections in stable timeline order`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Inspect the workspace"),
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-canonical"),
            connectionEpoch = 6,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:6:1",
                    text = "Partial answer",
                    reasoning = "Checking the workspace",
                    statusText = "Working",
                    status = AssistantTurnStatus.STREAMING,
                ),
                SessionTimelineItem.ToolActivity(
                    key = "tool:call-canonical",
                    toolId = "call-canonical",
                    name = "terminal",
                    output = "Loading",
                    status = ToolActivityStatus.RUNNING,
                ),
            ),
        )

        val initial = projector.project(baseline, realtime).single()
        val updated = projector.project(
            baseline,
            realtime.copy(
                timeline = listOf(
                    SessionTimelineItem.AssistantTurn(
                        key = "assistant:6:1",
                        text = "Partial answer with delta",
                        reasoning = "Checking the workspace",
                        statusText = "Working",
                        status = AssistantTurnStatus.STREAMING,
                    ),
                    SessionTimelineItem.ToolActivity(
                        key = "tool:call-canonical",
                        toolId = "call-canonical",
                        name = "terminal",
                        output = "Loading\ndone",
                        status = ToolActivityStatus.COMPLETE,
                    ),
                ),
            ),
        ).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.ACTIVITY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TOOL_GROUP,
            ),
            initial.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf(
                "turn:message:1:user-prompt",
                "turn:message:1:thinking:assistant:6:1:reasoning",
                "turn:message:1:activity:assistant:6:1:activity",
                "turn:message:1:response:assistant:6:1:response",
                "turn:message:1:tools:tool:call-canonical",
            ),
            initial.sections.map(HermesConversationSection::key),
        )
        assertEquals(
            initial.sections.map(HermesConversationSection::key),
            updated.sections.map(HermesConversationSection::key),
        )
        assertEquals(
            listOf(
                HermesConversationSectionStatus.COMPLETE,
                HermesConversationSectionStatus.COMPLETE,
                HermesConversationSectionStatus.COMPLETE,
                HermesConversationSectionStatus.COMPLETE,
                HermesConversationSectionStatus.RUNNING,
            ),
            initial.sections.map(HermesConversationSection::status),
        )
        assertEquals(
            "Partial answer with delta",
            (updated.sections[3] as HermesConversationSection.AssistantResponse).text,
        )
        assertEquals(
            "Loading\ndone",
            (updated.sections[4] as HermesConversationSection.ToolGroup).tools.single().output,
        )
    }

    @Test
    fun `realtime subagents and moa project one stable canonical process section`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Inspect the projector"),
        )
        val runtimeId = RuntimeSessionId("runtime-subagents")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 12)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "I’ll delegate this inspection.") },
            ),
            GatewayEvent(
                "subagent.start",
                runtimeId,
                buildJsonObject {
                    put("subagent_id", "child-1")
                    put("goal", "Inspect the projector")
                    put("model", "test-model")
                    put("task_index", 0)
                    put("task_count", 2)
                },
            ),
            GatewayEvent(
                "subagent.complete",
                runtimeId,
                buildJsonObject {
                    put("subagent_id", "child-1")
                    put("status", "completed")
                    put("summary", "Projector inspected")
                    put("duration_seconds", 1.5)
                    put("input_tokens", 100)
                    put("output_tokens", 25)
                    put("reasoning_tokens", 10)
                    put("api_calls", 3)
                },
            ),
            GatewayEvent(
                "moa.reference",
                runtimeId,
                buildJsonObject {
                    put("label", "Advisor 1")
                    put("text", "Reference answer")
                    put("index", 0)
                    put("count", 2)
                },
            ),
            GatewayEvent(
                "moa.phase",
                runtimeId,
                buildJsonObject {
                    put("phase", "aggregator")
                    put("aggregator", "test-model")
                    put("refs_done", 2)
                    put("refs_total", 2)
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Final synthesis") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(12, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.SUBAGENTS,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf("I’ll delegate this inspection.", "Final synthesis"),
            turn.sections.filterIsInstance<HermesConversationSection.AssistantResponse>().map { it.text },
        )
        val section = turn.sections[2] as HermesConversationSection.Subagents
        assertEquals("turn:message:1:subagents", section.key)
        assertEquals(HermesConversationSectionStatus.COMPLETE, section.status)
        assertEquals(
            HermesConversationSubagent(
                key = "assistant:12:1:subagent:child-1",
                goal = "Inspect the projector",
                status = HermesConversationSectionStatus.COMPLETE,
                model = "test-model",
                summary = "Projector inspected",
                durationSeconds = 1.5,
                taskIndex = 0,
                taskCount = 2,
                tokenSummary = HermesConversationTokenSummary(
                    inputTokens = 100,
                    outputTokens = 25,
                    reasoningTokens = 10,
                    totalTokens = 135,
                ),
                apiCalls = 3,
            ),
            section.subagents.single(),
        )
        assertEquals(
            HermesConversationMoaReference(
                key = "assistant:12:1:moa:0",
                label = "Advisor 1",
                text = "Reference answer",
            ),
            section.moaReferences.single(),
        )
        assertEquals(
            HermesConversationMoaProgress(
                phase = "aggregator",
                aggregator = "test-model",
                refsDone = 2,
                refsTotal = 2,
            ),
            section.moaProgress,
        )
    }

    @Test
    fun `v2 todo projection replaces one canonical mobile todo section`() {
        val baseline = transcript(message(id = 1, role = "user", content = "Run tests"))
        val runtimeId = RuntimeSessionId("runtime-v2-todo")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 13)
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                "todo.update",
                runtimeId,
                Json.parseToJsonElement(
                    """{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"Run tests","status":"in_progress"}]}""",
                ).jsonObject,
            ),
            EventCursor(13, 1),
        )
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                "todo.update",
                runtimeId,
                Json.parseToJsonElement(
                    """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Run tests","status":"completed"}]}""",
                ).jsonObject,
            ),
            EventCursor(13, 2),
        )

        val todo = projector.project(baseline, realtime)
            .single()
            .sections
            .filterIsInstance<HermesConversationSection.Todo>()
            .single()

        assertEquals(1, todo.items.size)
        assertEquals("Run tests", todo.items.single().content)
        assertEquals(HermesConversationTodoStatus.COMPLETED, todo.items.single().status)
    }

    @Test
    fun `realtime todo and inline diff become canonical ordered sections`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Update the implementation"),
        )
        val runtimeId = RuntimeSessionId("runtime-todo-diff")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 13)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "todo-1")
                    put("name", "todo")
                    put(
                        "todos",
                        buildJsonArray {
                            add(
                                buildJsonObject {
                                    put("id", "inspect")
                                    put("content", "Inspect the projector")
                                    put("status", "completed")
                                },
                            )
                            add(
                                buildJsonObject {
                                    put("id", "implement")
                                    put("content", "Implement canonical sections")
                                    put("status", "in_progress")
                                },
                            )
                        },
                    )
                    put("inline_diff", "- old\n+ new")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Implementation updated") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(13, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.TODO,
                HermesConversationSectionKind.DIFF,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf(
                HermesConversationTodoItem(
                    key = "inspect",
                    content = "Inspect the projector",
                    status = HermesConversationTodoStatus.COMPLETED,
                ),
                HermesConversationTodoItem(
                    key = "implement",
                    content = "Implement canonical sections",
                    status = HermesConversationTodoStatus.IN_PROGRESS,
                ),
            ),
            (turn.sections[2] as HermesConversationSection.Todo).items,
        )
        assertEquals(
            "- old\n+ new",
            (turn.sections[3] as HermesConversationSection.Diff).text,
        )
    }

    @Test
    fun `tool completion keeps derived sections after narration emitted while tool was running`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Update and explain"),
        )
        val runtimeId = RuntimeSessionId("runtime-late-tool-result")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 17)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "edit-1")
                    put("name", "patch")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "The edit is still running.") },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "edit-1")
                    put("name", "patch")
                    put(
                        "todos",
                        buildJsonArray {
                            add(
                                buildJsonObject {
                                    put("id", "edit")
                                    put("content", "Apply the edit")
                                    put("status", "completed")
                                },
                            )
                        },
                    )
                    put("inline_diff", "- before\n+ after")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "The edit is complete.") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(17, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TODO,
                HermesConversationSectionKind.DIFF,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf("The edit is still running.", "The edit is complete."),
            turn.sections.filterIsInstance<HermesConversationSection.AssistantResponse>().map { it.text },
        )
    }

    @Test
    fun `concurrent tool todo is emitted only at its owning completion marker`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Run both tools"),
        )
        val runtimeId = RuntimeSessionId("runtime-owned-todo")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 20)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-a")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-b")
                    put("name", "todo")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-a")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Tool A completed.") },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "tool-b")
                    put("name", "todo")
                    put(
                        "todos",
                        buildJsonArray {
                            add(
                                buildJsonObject {
                                    put("id", "both")
                                    put("content", "Run both tools")
                                    put("status", "completed")
                                },
                            )
                        },
                    )
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Both tools completed.") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(20, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TODO,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
    }

    @Test
    fun `message completion only reconciles the response segment after a tool`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Inspect then answer"),
        )
        val runtimeId = RuntimeSessionId("runtime-segment-completion")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 18)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "I’ll inspect first.") },
            ),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "inspect-1")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "inspect-1")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Partial final") },
            ),
            GatewayEvent(
                "message.complete",
                runtimeId,
                buildJsonObject { put("text", "Corrected final answer") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(18, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf("I’ll inspect first.", "Corrected final answer"),
            turn.sections.filterIsInstance<HermesConversationSection.AssistantResponse>().map { it.text },
        )
    }

    @Test
    fun `reasoning availability only reconciles the thinking segment after a tool`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Reason, inspect, then answer"),
        )
        val runtimeId = RuntimeSessionId("runtime-reasoning-completion")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 19)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "reasoning.delta",
                runtimeId,
                buildJsonObject { put("text", "Initial reasoning") },
            ),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "inspect-reasoning")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "inspect-reasoning")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "reasoning.delta",
                runtimeId,
                buildJsonObject { put("text", "Partial later reasoning") },
            ),
            GatewayEvent(
                "reasoning.available",
                runtimeId,
                buildJsonObject { put("text", "Corrected later reasoning") },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Final answer") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(19, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.THINKING,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf("Initial reasoning", "Corrected later reasoning"),
            turn.sections.filterIsInstance<HermesConversationSection.Thinking>().map { it.text },
        )
    }

    @Test
    fun `error after assistant text prevents terminal response boundary`() {
        val baseline = transcript(
            message(id = 1, role = "user", content = "Inspect safely"),
        )
        val runtimeId = RuntimeSessionId("runtime-error-after-response")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 21)
        val events = listOf(
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            GatewayEvent(
                "tool.start",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "inspect-error")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "tool.complete",
                runtimeId,
                buildJsonObject {
                    put("tool_id", "inspect-error")
                    put("name", "terminal")
                },
            ),
            GatewayEvent(
                "message.delta",
                runtimeId,
                buildJsonObject { put("text", "Partial answer") },
            ),
            GatewayEvent(
                "error",
                runtimeId,
                buildJsonObject { put("message", "Execution failed") },
            ),
        )
        events.forEachIndexed { index, event ->
            realtime = reducer.apply(realtime, event, EventCursor(21, (index + 1).toLong()))
        }

        val turn = projector.project(baseline, realtime).single()

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
                HermesConversationSectionKind.ERROR,
            ),
            turn.sections.map(HermesConversationSection::kind),
        )
    }

    @Test
    fun `historical encoded tool JSON restores canonical todo and diff sections`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Update the implementation"),
            message(
                id = 2,
                role = "tool",
                toolName = "todo",
                content = """
                    {
                      "todos": [
                        {"id":"inspect","content":"Inspect history","status":"completed"}
                      ],
                      "inline_diff": "- stale\n+ current"
                    }
                """.trimIndent(),
            ),
            message(id = 3, role = "assistant", content = "History restored"),
        )

        val sections = projector.project(transcript, realtime = null).single().sections

        assertEquals(
            listOf(
                HermesConversationSectionKind.USER_PROMPT,
                HermesConversationSectionKind.TOOL_GROUP,
                HermesConversationSectionKind.TODO,
                HermesConversationSectionKind.DIFF,
                HermesConversationSectionKind.RESPONSE_BOUNDARY,
                HermesConversationSectionKind.ASSISTANT_RESPONSE,
            ),
            sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            listOf(
                HermesConversationTodoItem(
                    key = "inspect",
                    content = "Inspect history",
                    status = HermesConversationTodoStatus.COMPLETED,
                ),
            ),
            (sections[2] as HermesConversationSection.Todo).items,
        )
        assertEquals(
            "- stale\n+ current",
            (sections[3] as HermesConversationSection.Diff).text,
        )
    }

    @Test
    fun `realtime assistant and tool activity form one streaming turn`() {
        val baseline = transcript(
            message(
                id = 1,
                role = "user",
                content = "Earlier question",
            ),
            message(
                id = 2,
                role = "assistant",
                content = "Earlier answer",
            ),
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 3,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:3:1",
                    text = "Partial answer",
                    reasoning = "Checking the facts",
                    thinking = "Reading sources",
                    statusText = "Working",
                    status = AssistantTurnStatus.STREAMING,
                ),
                SessionTimelineItem.ToolActivity(
                    key = "tool:call-live",
                    toolId = "call-live",
                    name = "browser",
                    args = "https://example.com",
                    output = "Loading",
                    status = ToolActivityStatus.RUNNING,
                ),
            ),
        )

        val turns = projector.project(transcript = baseline, realtime = realtime)

        assertEquals(2, turns.size)
        val liveTurn = turns.last()
        assertEquals("turn:assistant:3:1", liveTurn.key)
        assertEquals(null, liveTurn.userPrompt)
        assertEquals("Checking the facts\n\nReading sources", liveTurn.thinking)
        assertEquals("Working", liveTurn.statusText)
        assertEquals("Partial answer", liveTurn.response)
        assertEquals(ConversationTurnStatus.STREAMING, liveTurn.status)
        assertEquals("call-live", liveTurn.tools.single().toolId)
        assertEquals("Loading", liveTurn.tools.single().output)
        assertEquals(ConversationToolStatus.RUNNING, liveTurn.tools.single().status)
    }

    @Test
    fun `realtime preserves structured tool arguments and context across blank updates`() {
        val baseline = transcript()
        val runtimeId = RuntimeSessionId("runtime-structured")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(
            transcript = baseline,
            runtimeSessionId = runtimeId,
            connectionEpoch = 7,
        )
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                type = "tool.start",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("tool_id", "call-structured")
                    put("name", "terminal")
                    put(
                        "arguments",
                        buildJsonObject {
                            put("command", "pwd")
                            put("workdir", "/workspace")
                        },
                    )
                    put("context", buildJsonObject { put("path", "/workspace") })
                },
            ),
            EventCursor(7, 1),
        )
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                type = "tool.progress",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("tool_id", "call-structured")
                    put("arguments", "")
                    put("context", JsonNull)
                },
            ),
            EventCursor(7, 2),
        )

        val tool = projector.project(baseline, realtime).single().tools.single()

        assertEquals("Terminal(\"pwd\")", tool.callLabel)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Command", "pwd"),
                ConversationToolDetailUiModel("Workdir", "/workspace"),
            ),
            tool.argumentDetails,
        )
        assertEquals("Path: /workspace", tool.context)
    }

    @Test
    fun `realtime output deltas project only the canonical accumulated output`() {
        val baseline = transcript()
        val runtimeId = RuntimeSessionId("runtime-output")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 8)
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                type = "tool.output.delta",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("tool_id", "call-output")
                    put("name", "terminal")
                    put("output", "one\n")
                    put("stream", "stdout")
                    put("sequence", 1)
                },
            ),
            EventCursor(8, 1),
        )
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                type = "tool.output.delta",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("tool_id", "call-output")
                    put("text", "two")
                    put("stream", "stdout")
                    put("sequence", 2)
                },
            ),
            EventCursor(8, 2),
        )

        val tool = projector.project(baseline, realtime).single().tools.single()

        assertEquals("one\ntwo", tool.output)
        assertEquals(emptyList(), tool.resultDetails)
    }

    @Test
    fun `realtime keeps assistant completion when tool events arrive without message start`() {
        val baseline = transcript()
        val runtimeId = RuntimeSessionId("runtime-missing-start")
        val reducer = RealtimeSessionReducer()
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 9)
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                type = "message.complete",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject { put("text", "Answer without start") },
            ),
            EventCursor(9, 1),
        )
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                type = "tool.start",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("tool_id", "late-tool")
                    put("name", "terminal")
                    put("arguments", buildJsonObject { put("command", "pwd") })
                },
            ),
            EventCursor(9, 2),
        )

        val turn = projector.project(baseline, realtime).single()

        assertEquals("Answer without start", turn.response)
        assertEquals(ConversationTurnStatus.COMPLETE, turn.status)
        assertEquals("Terminal(\"pwd\")", turn.tools.single().callLabel)
    }

    @Test
    fun `realtime JSON strings use the same readable tool presentation as history`() {
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 5,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.ToolActivity(
                    key = "tool:search-1",
                    toolId = "search-1",
                    name = "search_files",
                    args = """{"pattern":"display","path":"/workspace"}""",
                    result = """{"message":"1 match","total_count":1}""",
                    durationSeconds = 0.4,
                    status = ToolActivityStatus.COMPLETE,
                ),
            ),
        )

        val tool = projector.project(baseline, realtime).single().tools.single()

        assertEquals("Search Files(\"/workspace\")", tool.callLabel)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Pattern", "display"),
                ConversationToolDetailUiModel("Path", "/workspace"),
            ),
            tool.argumentDetails,
        )
        assertEquals("1 match", tool.result)
        assertEquals(
            listOf(ConversationToolDetailUiModel("Total count", "1")),
            tool.resultDetails,
        )
    }

    @Test
    fun `structured realtime object arguments remain readable through projection`() {
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-structured-args"),
            connectionEpoch = 5,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.ToolActivity(
                    key = "tool:call-structured",
                    toolId = "call-structured",
                    name = "terminal",
                    status = ToolActivityStatus.RUNNING,
                    payload = buildJsonObject {
                        put("tool_id", "call-structured")
                        put("name", "terminal")
                        put(
                            "arguments",
                            buildJsonObject {
                                put("command", "pwd")
                                put("workdir", "/workspace")
                            },
                        )
                    },
                ),
            ),
        )

        val tool = projector.project(baseline, realtime).single().tools.single()

        assertEquals("Terminal(\"pwd\")", tool.callLabel)
        assertEquals("Command: pwd\nWorkdir: /workspace", tool.arguments)
        assertEquals(
            listOf(
                ConversationToolDetailUiModel("Command", "pwd"),
                ConversationToolDetailUiModel("Workdir", "/workspace"),
            ),
            tool.argumentDetails,
        )
    }

    @Test
    fun `structured realtime preserves unknown tool result fields as readable details`() {
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-unknown-result"),
            connectionEpoch = 2,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.ToolActivity(
                    key = "tool:call-unknown",
                    toolId = "call-unknown",
                    name = "terminal",
                    summary = "short summary",
                    status = ToolActivityStatus.COMPLETE,
                    payload = buildJsonObject {
                        put("tool_id", "call-unknown")
                        put("name", "terminal")
                        put("output", "done")
                        put("summary", "short summary")
                        put("trace_id", "trace-123")
                    },
                ),
            ),
        )

        val tool = projector.project(baseline, realtime).single().tools.single()

        assertEquals("done", tool.output)
        assertEquals(null, tool.summary)
        assertTrue(tool.resultDetails.contains(ConversationToolDetailUiModel("Summary", "short summary")))
        assertTrue(tool.resultDetails.contains(ConversationToolDetailUiModel("Trace id", "trace-123")))
    }

    @Test
    fun `blank canonical payload fields preserve nonblank tool fallbacks`() {
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-blank-canonical"),
            connectionEpoch = 2,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.ToolActivity(
                    key = "tool:call-blank",
                    toolId = "call-blank",
                    name = "terminal",
                    output = "fallback output",
                    result = "fallback result",
                    summary = "fallback summary",
                    diff = "fallback diff",
                    error = "fallback error",
                    status = ToolActivityStatus.ERROR,
                    payload = buildJsonObject {
                        put("tool_id", "call-blank")
                        put("output", "")
                        put("result", JsonNull)
                        put("summary", "")
                        put("diff", "")
                        put("error", "")
                        put("trace_id", "trace-blank")
                    },
                ),
            ),
        )

        val tool = projector.project(baseline, realtime).single().tools.single()

        assertEquals("fallback output", tool.output)
        assertEquals("fallback result", tool.result)
        assertEquals("fallback summary", tool.summary)
        assertEquals("fallback diff", tool.diff)
        assertEquals("fallback error", tool.error)
        assertTrue(tool.resultDetails.none { it.value.isBlank() }, tool.resultDetails.toString())
        assertTrue(tool.resultDetails.contains(ConversationToolDetailUiModel("Trace id", "trace-blank")))
    }

    @Test
    fun `structured realtime sanitizes assistant errors and status payloads`() {
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-safe-status"),
            connectionEpoch = 3,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.StatusActivity(
                    key = "status-safe",
                    text = """{"message":"running","access_token":"status-sensitive-value"}""",
                ),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-error-safe",
                    status = AssistantTurnStatus.ERROR,
                    error = "Authorization: " + "Bearer " + "error-sensitive-value",
                ),
            ),
        )

        val turns = projector.project(baseline, realtime)

        assertTrue(turns.first().statusText.contains("running"))
        assertFalse(turns.first().statusText.contains('{'))
        assertFalse(turns.first().statusText.contains("status-sensitive-value"))
        assertTrue(turns.last().error.orEmpty().contains("[redacted]"))
        assertFalse(turns.last().error.orEmpty().contains("error-sensitive-value"))
    }

    @Test
    fun `status activity kind preserves informational warning and error tone`() {
        val baseline = transcript()

        fun projectedActivity(kind: String): HermesConversationSection.Activity {
            val realtime = RealtimeSessionReducer().seed(
                transcript = baseline,
                runtimeSessionId = RuntimeSessionId("runtime-$kind"),
                connectionEpoch = 1,
            ).copy(
                timeline = listOf(
                    SessionTimelineItem.StatusActivity(
                        key = "status-$kind",
                        kind = kind,
                        text = "$kind activity",
                    ),
                ),
            )
            return projector.project(baseline, realtime)
                .single()
                .sections
                .filterIsInstance<HermesConversationSection.Activity>()
                .single()
        }

        val info = projectedActivity("info")
        val warning = projectedActivity("warning")
        val error = projectedActivity("error")

        assertEquals(HermesConversationActivityTone.INFO, info.tone)
        assertEquals(HermesConversationSectionStatus.INTERRUPTED, info.status)
        assertEquals(HermesConversationActivityTone.WARNING, warning.tone)
        assertEquals(HermesConversationSectionStatus.INTERRUPTED, warning.status)
        assertEquals(HermesConversationActivityTone.ERROR, error.tone)
        assertEquals(HermesConversationSectionStatus.ERROR, error.status)
    }

    @Test
    fun `reducer status updates are humanized and redacted before projection`() {
        val baseline = transcript()
        val reducer = RealtimeSessionReducer()
        val runtimeId = RuntimeSessionId("runtime-status-update")
        var realtime = reducer.seed(baseline, runtimeId, connectionEpoch = 7)
        realtime = reducer.apply(
            realtime,
            GatewayEvent("message.start", runtimeId, buildJsonObject {}),
            EventCursor(7, 1),
        )
        realtime = reducer.apply(
            realtime,
            GatewayEvent(
                "status.update",
                runtimeId,
                buildJsonObject {
                    put(
                        "text",
                        """{"message":"running","access_token":"status-sensitive-value"}""",
                    )
                },
            ),
            EventCursor(7, 2),
        )

        val status = projector.project(baseline, realtime).single().statusText

        assertTrue(status.contains("running"), status)
        assertFalse(status.contains('{'), status)
        assertFalse(status.contains("status-sensitive-value"), status)
        assertTrue(status.contains("[redacted]"), status)
    }

    @Test
    fun `realtime user and assistant stay in one conversation turn`() {
        val baseline = transcript()
        val realtime = RealtimeSessionReducer().seed(
            transcript = baseline,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 4,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.User(
                    key = "user:4:1",
                    text = "What is the current directory?",
                ),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant:4:1",
                    text = "It is `/workspace`.",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )

        val turn = projector.project(baseline, realtime).single()

        assertEquals("What is the current directory?", turn.userPrompt?.text)
        assertEquals("It is `/workspace`.", turn.response)
        assertEquals(ConversationTurnStatus.STREAMING, turn.status)
    }

    @Test
    fun `historical tool result without tool call id remains visible under a stable fallback key`() {
        val transcript = transcript(
            message(
                id = 41,
                role = "tool",
                content = "partial historical output",
                toolName = "terminal",
            ),
        )

        val initial = projector.project(transcript, realtime = null).single()
        val replayed = projector.project(transcript, realtime = null).single()
        val tool = initial.tools.single()

        assertEquals("message:41:tool-result", tool.toolId)
        assertEquals("tool:message:41:tool-result", tool.key)
        assertEquals("partial historical output", tool.output)
        assertEquals(ConversationToolStatus.COMPLETE, tool.status)
        assertEquals(
            listOf(HermesConversationSectionKind.TOOL_GROUP),
            initial.sections.map(HermesConversationSection::kind),
        )
        assertEquals(
            initial.tools.map(ConversationToolUiModel::key),
            replayed.tools.map(ConversationToolUiModel::key),
        )
        assertEquals(
            initial.sections.map(HermesConversationSection::key),
            replayed.sections.map(HermesConversationSection::key),
        )
    }

    @Test
    fun `partial history window preserves a leading tool result and keeps the completed turn key`() {
        val partial = transcript(
            message(
                id = 3,
                role = "tool",
                content = "/workspace\n",
                toolCallId = "call-1",
                toolName = "terminal",
            ),
            message(
                id = 4,
                role = "assistant",
                content = "The workspace is `/workspace`.",
            ),
        )
        val complete = transcript(
            message(id = 1, role = "user", content = "Inspect the workspace"),
            message(
                id = 2,
                role = "assistant",
                content = "",
                toolCalls = buildJsonArray {
                    add(
                        buildJsonObject {
                            put("id", "call-1")
                            put(
                                "function",
                                buildJsonObject {
                                    put("name", "terminal")
                                    put("arguments", "{\"command\":\"pwd\"}")
                                },
                            )
                        },
                    )
                },
            ),
            *partial.messages.toTypedArray(),
        )

        val partialTurn = projector.project(partial, realtime = null).single()
        val completeTurn = projector.project(complete, realtime = null).single()

        assertEquals("/workspace\n", partialTurn.tools.single().output)
        assertEquals(completeTurn.key, partialTurn.key)
        assertEquals("turn:response:4", partialTurn.key)
    }

    @Test
    fun `hidden display messages never become user prompts`() {
        val hidden = message(
            id = 1,
            role = "user",
            content = "internal compacted handoff",
        ).copy(displayKind = "hidden")
        val transcript = transcript(
            hidden,
            message(id = 2, role = "user", content = "Visible question"),
            message(id = 3, role = "assistant", content = "Visible answer"),
        )

        val turn = projector.project(transcript, realtime = null).single()

        assertEquals("Visible question", turn.userPrompt?.text)
        assertEquals("Visible answer", turn.response)
    }

    @Test
    fun `historical system note warn and error rows project as ordered standalone turns`() {
        val transcript = transcript(
            message(id = 10, role = "system", content = "System checkpoint"),
            message(id = 11, role = "note", content = "Saved note"),
            message(id = 12, role = "warn", content = "Connection is unstable"),
            message(id = 13, role = "error", content = "Execution interrupted"),
            message(id = 14, role = "system", content = "hidden internal row")
                .copy(displayKind = "hidden"),
        )

        val turns = projector.project(transcript, realtime = null)

        assertEquals(
            listOf("turn:event:10", "turn:event:11", "turn:event:12", "turn:error:13"),
            turns.map(ConversationTurnUiModel::key),
        )
        assertEquals(
            listOf("System checkpoint", "Saved note", "Connection is unstable"),
            turns.take(3).map(ConversationTurnUiModel::eventText),
        )
        assertEquals("Execution interrupted", turns.last().error)
        assertEquals(ConversationTurnStatus.ERROR, turns.last().status)
        assertTrue(turns.all { it.userPrompt == null })
    }

    @Test
    fun `Hermes display events project as timeline markers instead of user prompts`() {
        val transcript = transcript(
            message(id = 1, role = "user", content = "Visible question"),
            message(
                id = 2,
                role = "user",
                content = "[System: model changed to gpt-5]",
            ).copy(displayKind = "model_switch"),
            message(id = 3, role = "assistant", content = "Visible answer"),
            message(
                id = 4,
                role = "user",
                content = "[IMPORTANT: delegation done]",
            ).copy(
                displayKind = "async_delegation_complete",
                displayMetadata = buildJsonObject { put("task_count", 2) },
            ),
        )

        val turns = projector.project(transcript, realtime = null)

        assertEquals(4, turns.size)
        assertEquals("Visible question", turns[0].userPrompt?.text)
        assertEquals("model changed", turns[1].eventText)
        assertEquals(null, turns[1].userPrompt)
        assertEquals("Visible answer", turns[2].response)
        assertEquals("2 background agents finished", turns[3].eventText)
        assertEquals(null, turns[3].userPrompt)
    }

    private fun transcript(vararg messages: SessionMessageProjection) = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = SessionKey("stored-1"),
        messages = messages.toList(),
        pagination = TranscriptPagination(
            limit = messages.size,
            offset = 0,
            returned = messages.size,
        ),
    )

    private fun message(
        id: Long,
        role: String,
        content: String,
        reasoning: String? = null,
        toolCallId: String? = null,
        toolCalls: kotlinx.serialization.json.JsonElement? = null,
        toolName: String? = null,
    ) = SessionMessageProjection(
        messageId = id,
        role = role,
        content = JsonPrimitive(content),
        timestampEpochSeconds = id.toDouble(),
        reasoning = reasoning,
        reasoningContent = null,
        reasoningDetails = null,
        toolCallId = toolCallId,
        toolCalls = toolCalls,
        toolName = toolName,
        displayKind = null,
        displayMetadata = null,
    )
}
