package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.GatewayEvent
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionTranscript
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

class RealtimeSessionReducerTest {
    private val reducer = RealtimeSessionReducer()
    private val runtimeId = RuntimeSessionId("runtime-1")
    private val sessionKey = SessionKey("stored-1")

    @Test
    fun `assistant deltas append in order and a duplicate cursor is ignored`() {
        var state = seed(connectionEpoch = 1)

        state = reducer.apply(state, event("message.start"), EventCursor(1, 1))
        state = reducer.apply(state, event("message.delta", "text" to "first\n"), EventCursor(1, 2))
        state = reducer.apply(state, event("message.delta", "text" to "second"), EventCursor(1, 3))
        state = reducer.apply(state, event("message.delta", "text" to "second"), EventCursor(1, 3))

        val turn = assertIs<SessionTimelineItem.AssistantTurn>(state.timeline.single())
        assertEquals("first\nsecond", turn.text)
        assertEquals(AssistantTurnStatus.STREAMING, turn.status)
        assertEquals("assistant:1:1", turn.key)
    }

    @Test
    fun `v2 live updates project todo subagent tool and terminal by composite identity`() {
        var state = seed(connectionEpoch = 1)
        val updates = listOf(
            v2Event(
                "todo.update",
                1,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"Run tests","status":"in_progress"}]}""",
            ),
            v2Event(
                "subagent.update",
                2,
                """{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":2,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"Run tests","summary":null,"status":"running"}""",
            ),
            v2Event(
                "tool.update",
                3,
                """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":3,"operation":"upsert","status":"running","name":"Tests"}""",
            ),
            v2Event(
                "terminal.update",
                4,
                """{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":4,"operation":"upsert","status":"running"}""",
            ),
        )
        updates.forEachIndexed { index, update ->
            state = reducer.apply(state, update, EventCursor(1, (index + 1).toLong()))
        }

        assertEquals(4L, state.lastEventOrdinal)
        assertEquals(
            V2LifecycleProjectionKey.encode(
                "subagent",
                V2LifecycleIdentity("turn-1", "agent-1"),
            ),
            state.subagents.single().key,
        )
        assertEquals(
            V2LifecycleProjectionKey.encode(
                "tool",
                V2LifecycleIdentity("turn-1", "tool-1"),
            ),
            state.tools.single().key,
        )
        assertTrue(state.toString().contains("todo-1"))
        assertTrue(state.toString().contains("process-1"))
    }

    @Test
    fun `v2 todo update is a stable replacement and revision gaps fail closed`() {
        var state = reducer.apply(
            seed(connectionEpoch = 1),
            v2Event(
                "todo.update",
                1,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"Run tests","status":"in_progress"}]}""",
            ),
            EventCursor(1, 1),
        )
        state = reducer.apply(
            state,
            v2Event(
                "todo.update",
                2,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Run tests","status":"completed"}]}""",
            ),
            EventCursor(1, 2),
        )

        assertTrue(state.toString().contains("revision=2"))
        assertTrue(state.toString().contains("COMPLETED"))
        val beforeGap = state
        val afterGap = reducer.apply(
            state,
            v2Event(
                "todo.update",
                3,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":4,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"Run tests","status":"completed"}]}""",
            ),
            EventCursor(1, 3),
        )
        assertSame(beforeGap, afterGap)
    }

    @Test
    fun `v2 subagent graph rejects orphan cycle depth nine and node 129`() {
        var state = seed(connectionEpoch = 1)
        val orphan = reducer.apply(
            state,
            v2Event(
                "subagent.update",
                1,
                """{"turn_id":"turn-1","subagent_id":"orphan","revision":1,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":"missing","name":"Orphan","goal":"","summary":null,"status":"running"}""",
            ),
            EventCursor(1, 1),
        )
        assertSame(state, orphan)

        repeat(8) { index ->
            val sequence = (index + 1).toLong()
            val parent = if (index == 0) "null" else "\"agent-${index - 1}\""
            state = reducer.apply(
                state,
                v2Event(
                    "subagent.update",
                    sequence,
                    """{"turn_id":"turn-1","subagent_id":"agent-$index","revision":1,"first_event_sequence":$sequence,"operation":"upsert","parent_subagent_id":$parent,"name":"Agent","goal":"","summary":null,"status":"running"}""",
                ),
                EventCursor(1, sequence),
            )
        }
        assertEquals(8, state.subagents.size)
        val tooDeep = reducer.apply(
            state,
            v2Event(
                "subagent.update",
                9,
                """{"turn_id":"turn-1","subagent_id":"agent-8","revision":1,"first_event_sequence":9,"operation":"upsert","parent_subagent_id":"agent-7","name":"Too deep","goal":"","summary":null,"status":"running"}""",
            ),
            EventCursor(1, 9),
        )
        assertSame(state, tooDeep)

        val cycle = reducer.apply(
            state,
            v2Event(
                "subagent.update",
                9,
                """{"turn_id":"turn-1","subagent_id":"agent-0","revision":2,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":"agent-7","name":"Agent","goal":"","summary":null,"status":"running"}""",
            ),
            EventCursor(1, 9),
        )
        assertSame(state, cycle)

        var wide = seed(connectionEpoch = 2)
        repeat(128) { index ->
            val sequence = (index + 1).toLong()
            wide = reducer.apply(
                wide,
                v2Event(
                    "subagent.update",
                    sequence,
                    """{"turn_id":"turn-wide","subagent_id":"agent-$index","revision":1,"first_event_sequence":$sequence,"operation":"upsert","parent_subagent_id":null,"name":"Agent","goal":"","summary":null,"status":"running"}""",
                ),
                EventCursor(2, sequence),
            )
        }
        assertEquals(128, wide.subagents.size)
        val overflow = reducer.apply(
            wide,
            v2Event(
                "subagent.update",
                129,
                """{"turn_id":"turn-wide","subagent_id":"agent-128","revision":1,"first_event_sequence":129,"operation":"upsert","parent_subagent_id":null,"name":"Overflow","goal":"","summary":null,"status":"running"}""",
            ),
            EventCursor(2, 129),
        )
        assertSame(wide, overflow)
    }

    @Test
    fun `v2 tool and terminal lifecycle updates preserve safe streamed output on one node`() {
        var state = seed(connectionEpoch = 1)
        state = reducer.apply(
            state,
            v2Event(
                "tool.update",
                1,
                """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"running","name":"Tests"}""",
            ),
            EventCursor(1, 1),
        )
        state = reducer.apply(
            state,
            v2Event(
                "tool.output.delta",
                2,
                """{"turn_id":"turn-1","tool_call_id":"tool-1","text":"safe tool output","sequence":1}""",
            ),
            EventCursor(1, 2),
        )
        state = reducer.apply(
            state,
            v2Event(
                "tool.update",
                3,
                """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","name":"Tests","summary":"Passed"}""",
            ),
            EventCursor(1, 3),
        )
        state = reducer.apply(
            state,
            v2Event(
                "terminal.update",
                4,
                """{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":4,"operation":"upsert","status":"running"}""",
            ),
            EventCursor(1, 4),
        )
        state = reducer.apply(
            state,
            v2Event(
                "agent.terminal.output",
                5,
                """{"turn_id":"turn-1","process_id":"process-1","stream":"stdout","text":"safe terminal output","sequence":1}""",
            ),
            EventCursor(1, 5),
        )
        state = reducer.apply(
            state,
            v2Event(
                "terminal.update",
                6,
                """{"turn_id":"turn-1","process_id":"process-1","revision":2,"first_event_sequence":4,"operation":"upsert","status":"completed","exit_code":0,"summary":"Done"}""",
            ),
            EventCursor(1, 6),
        )

        assertEquals(1, state.tools.size)
        assertEquals(LiveToolStatus.COMPLETE, state.tools.single().status)
        assertEquals(1, state.terminals.size)
        assertEquals(LiveToolStatus.COMPLETE, state.terminals.single().status)
        val activities = state.timeline.filterIsInstance<SessionTimelineItem.ToolActivity>()
        assertEquals(2, activities.size)
        assertEquals("safe tool output", activities.single { it.toolId.startsWith("v2|tool|") }.output)
        assertEquals(
            "safe terminal output",
            activities.single { it.toolId.startsWith("v2|terminal|") }.output,
        )
    }

    @Test
    fun `v2 todo keeps existing ids labels and order and deletes only when every item is terminal`() {
        val seeded = reducer.apply(
            seed(connectionEpoch = 1),
            v2Event(
                "todo.update",
                1,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":1,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"First","status":"in_progress"},{"id":"item-2","label":"Second","status":"pending"}]}""",
            ),
            EventCursor(1, 1),
        )
        listOf(
            """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"First","status":"in_progress"}]}""",
            """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-2","label":"Second","status":"pending"},{"id":"item-1","label":"First","status":"in_progress"}]}""",
            """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"in_progress","items":[{"id":"item-1","label":"Renamed","status":"in_progress"},{"id":"item-2","label":"Second","status":"pending"}]}""",
        ).forEach { invalidPayload ->
            assertSame(
                seeded,
                reducer.apply(
                    seeded,
                    v2Event("todo.update", 2, invalidPayload),
                    EventCursor(1, 2),
                ),
            )
        }
        assertSame(
            seeded,
            reducer.apply(
                seeded,
                v2Event(
                    "todo.update",
                    2,
                    """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"delete"}""",
                ),
                EventCursor(1, 2),
            ),
        )
        val completed = reducer.apply(
            seeded,
            v2Event(
                "todo.update",
                2,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":2,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"item-1","label":"First","status":"completed"},{"id":"item-2","label":"Second","status":"cancelled"},{"id":"item-3","label":"Appended","status":"completed"}]}""",
            ),
            EventCursor(1, 2),
        )
        assertEquals(listOf("item-1", "item-2", "item-3"), completed.todoSections.single().items.map { it.key })

        val deleted = reducer.apply(
            completed,
            v2Event(
                "todo.update",
                3,
                """{"turn_id":"turn-1","section_id":"todo-1","revision":3,"first_event_sequence":1,"operation":"delete"}""",
            ),
            EventCursor(1, 3),
        )
        assertTrue(deleted.todoSections.isEmpty())
    }

    @Test
    fun `v2 absorbing lifecycle entities only enrich missing safe metadata`() {
        var state = seed(connectionEpoch = 1)
        state = reducer.apply(
            state,
            v2Event(
                "subagent.update",
                1,
                """{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"Run tests","summary":"Done","status":"completed","model":"model-a","duration_ms":10,"api_calls":1}""",
            ),
            EventCursor(1, 1),
        )
        state = reducer.apply(
            state,
            v2Event(
                "tool.update",
                2,
                """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":1,"first_event_sequence":2,"operation":"upsert","status":"completed","name":"Tests","summary":"Passed","duration_ms":10}""",
            ),
            EventCursor(1, 2),
        )
        state = reducer.apply(
            state,
            v2Event(
                "terminal.update",
                3,
                """{"turn_id":"turn-1","process_id":"process-1","revision":1,"first_event_sequence":3,"operation":"upsert","status":"failed","exit_code":1,"summary":"Failed","duration_ms":10}""",
            ),
            EventCursor(1, 3),
        )

        val subagentRewrite = reducer.apply(
            state,
            v2Event(
                "subagent.update",
                4,
                """{"turn_id":"turn-1","subagent_id":"agent-1","revision":2,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Renamed","goal":"Different","summary":"Changed","status":"completed","model":"model-b","duration_ms":20,"api_calls":2}""",
            ),
            EventCursor(1, 4),
        )
        val toolRewrite = reducer.apply(
            state,
            v2Event(
                "tool.update",
                4,
                """{"turn_id":"turn-1","tool_call_id":"tool-1","revision":2,"first_event_sequence":2,"operation":"upsert","status":"completed","name":"Renamed","summary":"Changed","duration_ms":20}""",
            ),
            EventCursor(1, 4),
        )
        val terminalRewrite = reducer.apply(
            state,
            v2Event(
                "terminal.update",
                4,
                """{"turn_id":"turn-1","process_id":"process-1","revision":2,"first_event_sequence":3,"operation":"upsert","status":"failed","exit_code":2,"summary":"Changed","duration_ms":20}""",
            ),
            EventCursor(1, 4),
        )

        assertSame(state, subagentRewrite)
        assertSame(state, toolRewrite)
        assertSame(state, terminalRewrite)
    }

    @Test
    fun `v2 live lifecycle identities remain distinct when delimiter text collides`() {
        var state = seed(connectionEpoch = 1)
        var sequence = 1L
        fun apply(type: String, payload: String) {
            state = reducer.apply(
                state,
                v2Event(type, sequence, payload),
                EventCursor(1, sequence),
            )
            sequence += 1
        }

        apply(
            "todo.update",
            """{"turn_id":"a","section_id":"b:todo:c","revision":1,"first_event_sequence":1,"operation":"upsert","status":"completed","items":[{"id":"one","label":"First","status":"completed"}]}""",
        )
        apply(
            "todo.update",
            """{"turn_id":"a:todo:b","section_id":"c","revision":1,"first_event_sequence":2,"operation":"upsert","status":"completed","items":[{"id":"two","label":"Second","status":"completed"}]}""",
        )
        assertEquals(2, state.todoSections.size)
        assertEquals(2, state.todoSections.map { it.key }.toSet().size)

        apply(
            "subagent.update",
            """{"turn_id":"a","subagent_id":"b:subagent:c","revision":1,"first_event_sequence":3,"operation":"upsert","parent_subagent_id":null,"name":"Parent A","goal":"","summary":null,"status":"completed"}""",
        )
        apply(
            "subagent.update",
            """{"turn_id":"a:subagent:b","subagent_id":"c","revision":1,"first_event_sequence":4,"operation":"upsert","parent_subagent_id":null,"name":"Parent B","goal":"","summary":null,"status":"completed"}""",
        )
        apply(
            "subagent.update",
            """{"turn_id":"a","subagent_id":"child","revision":1,"first_event_sequence":5,"operation":"upsert","parent_subagent_id":"b:subagent:c","name":"Child A","goal":"","summary":null,"status":"completed"}""",
        )
        apply(
            "subagent.update",
            """{"turn_id":"a:subagent:b","subagent_id":"child","revision":1,"first_event_sequence":6,"operation":"upsert","parent_subagent_id":"c","name":"Child B","goal":"","summary":null,"status":"completed"}""",
        )
        assertEquals(4, state.subagents.size)
        assertEquals(4, state.subagents.map { it.key }.toSet().size)
        assertEquals(2, state.subagents.mapNotNull { it.parentKey }.toSet().size)

        apply(
            "tool.update",
            """{"turn_id":"a","tool_call_id":"b:tool:c","revision":1,"first_event_sequence":7,"operation":"upsert","status":"completed","name":"Tool A"}""",
        )
        apply(
            "tool.update",
            """{"turn_id":"a:tool:b","tool_call_id":"c","revision":1,"first_event_sequence":8,"operation":"upsert","status":"completed","name":"Tool B"}""",
        )
        assertEquals(2, state.tools.size)
        assertEquals(2, state.tools.map { it.key }.toSet().size)

        apply(
            "terminal.update",
            """{"turn_id":"a","process_id":"b:terminal:c","revision":1,"first_event_sequence":9,"operation":"upsert","status":"completed","exit_code":0}""",
        )
        apply(
            "terminal.update",
            """{"turn_id":"a:terminal:b","process_id":"c","revision":1,"first_event_sequence":10,"operation":"upsert","status":"completed","exit_code":0}""",
        )
        assertEquals(2, state.terminals.size)
        assertEquals(2, state.terminals.map { it.key }.toSet().size)

        listOf(
            "todo.update" to """{"turn_id":"a","section_id":"b:todo:c","revision":2,"first_event_sequence":1,"operation":"delete"}""",
            "todo.update" to """{"turn_id":"a:todo:b","section_id":"c","revision":2,"first_event_sequence":2,"operation":"delete"}""",
            "subagent.update" to """{"turn_id":"a","subagent_id":"child","revision":2,"first_event_sequence":5,"operation":"delete"}""",
            "subagent.update" to """{"turn_id":"a:subagent:b","subagent_id":"child","revision":2,"first_event_sequence":6,"operation":"delete"}""",
            "subagent.update" to """{"turn_id":"a","subagent_id":"b:subagent:c","revision":2,"first_event_sequence":3,"operation":"delete"}""",
            "subagent.update" to """{"turn_id":"a:subagent:b","subagent_id":"c","revision":2,"first_event_sequence":4,"operation":"delete"}""",
            "tool.update" to """{"turn_id":"a","tool_call_id":"b:tool:c","revision":2,"first_event_sequence":7,"operation":"delete"}""",
            "tool.update" to """{"turn_id":"a:tool:b","tool_call_id":"c","revision":2,"first_event_sequence":8,"operation":"delete"}""",
            "terminal.update" to """{"turn_id":"a","process_id":"b:terminal:c","revision":2,"first_event_sequence":9,"operation":"delete"}""",
            "terminal.update" to """{"turn_id":"a:terminal:b","process_id":"c","revision":2,"first_event_sequence":10,"operation":"delete"}""",
        ).forEach { (type, payload) -> apply(type, payload) }

        assertTrue(state.todoSections.isEmpty())
        assertTrue(state.subagents.isEmpty())
        assertTrue(state.tools.isEmpty())
        assertTrue(state.terminals.isEmpty())
    }

    @Test
    fun `v2 subagent presentation preserves values above Int max as Long`() {
        val beyondInt = Int.MAX_VALUE.toLong() + 1
        val state = reducer.apply(
            seed(connectionEpoch = 1),
            v2Event(
                "subagent.update",
                1,
                """{"turn_id":"turn-1","subagent_id":"agent-1","revision":1,"first_event_sequence":1,"operation":"upsert","parent_subagent_id":null,"name":"Runner","goal":"","summary":null,"status":"running","progress":{"current":$beyondInt,"total":$beyondInt},"api_calls":$beyondInt}""",
            ),
            EventCursor(1, 1),
        )

        val projection = state.subagents.single()
        assertEquals(beyondInt, projection.taskIndex)
        assertEquals(beyondInt, projection.taskCount)
        assertEquals(beyondInt, projection.apiCalls)
    }

    @Test
    fun `v2 lifecycle UI keys round trip delimiter text without collisions`() {
        listOf("todo", "subagent", "tool", "terminal").forEach { kind ->
            val first = V2LifecycleIdentity("a", "b:$kind:c")
            val second = V2LifecycleIdentity("a:$kind:b", "c")
            val firstKey = V2LifecycleProjectionKey.encode(kind, first)
            val secondKey = V2LifecycleProjectionKey.encode(kind, second)

            assertTrue(firstKey != secondKey)
            assertEquals(kind to first, V2LifecycleProjectionKey.decode(firstKey))
            assertEquals(kind to second, V2LifecycleProjectionKey.decode(secondKey))
        }
    }

    @Test
    fun `unknown event does not advance the authoritative cursor`() {
        val state = seed(connectionEpoch = 1)

        val updated = reducer.apply(
            state,
            event("future.event", "text" to "must not advance"),
            EventCursor(1, 1),
        )

        assertSame(state, updated)
        assertEquals(0L, updated.lastEventOrdinal)
    }

    @Test
    fun `assistant token replacement identifies its source timeline and changed index`() {
        val started = reducer.apply(
            seed(connectionEpoch = 1),
            event("message.start"),
            EventCursor(1, 1),
        )

        val updated = reducer.apply(
            started,
            event("message.delta", "text" to "first"),
            EventCursor(1, 2),
        )

        assertSame(started.timeline, updated.timelineMutation.sourceTimeline)
        assertEquals(started.timeline.lastIndex, updated.timelineMutation.firstChangedIndex)
    }

    @Test
    fun `assistant token replacement does not copy the settled live timeline prefix`() {
        val timeline = TimelineReadCountingList(
            buildList {
                repeat(150) { index ->
                    add(SessionTimelineItem.User(key = "user-$index", text = "Question $index"))
                    add(
                        SessionTimelineItem.AssistantTurn(
                            key = "assistant-$index",
                            text = "Answer $index",
                            status = AssistantTurnStatus.COMPLETE,
                        ),
                    )
                }
                add(
                    SessionTimelineItem.AssistantTurn(
                        key = "assistant-active",
                        turnKey = "assistant-active",
                        segmentKind = AssistantSegmentKind.RESPONSE,
                        text = "Streaming",
                        status = AssistantTurnStatus.STREAMING,
                    ),
                )
            },
        )
        val state = seed(connectionEpoch = 1).copy(
            running = true,
            activeAssistantTurnKey = "assistant-active",
            timeline = timeline,
        )

        val updated = reducer.apply(
            state,
            event("message.delta", "text" to " answer"),
            EventCursor(1, 1),
        )

        assertEquals("Streaming answer", (updated.timeline.last() as SessionTimelineItem.AssistantTurn).text)
        assertTrue(
            timeline.readCount <= 8,
            "Reducer read ${timeline.readCount} settled live timeline items for one token.",
        )
    }

    @Test
    fun `repeated tool output replacement does not scan the settled live timeline prefix`() {
        val timeline = TimelineReadCountingList(
            buildList {
                repeat(150) { index ->
                    add(SessionTimelineItem.User(key = "user-$index", text = "Question $index"))
                    add(
                        SessionTimelineItem.AssistantTurn(
                            key = "assistant-$index",
                            text = "Answer $index",
                            status = AssistantTurnStatus.COMPLETE,
                        ),
                    )
                }
                add(
                    SessionTimelineItem.ToolActivity(
                        key = "tool:call-tail",
                        toolId = "call-tail",
                        name = "terminal",
                        status = ToolActivityStatus.RUNNING,
                        payload = buildJsonObject {},
                    ),
                )
            },
        )
        val state = seed(connectionEpoch = 1).copy(
            running = true,
            timeline = timeline,
        )
        val first = reducer.apply(
            state,
            eventWithSequence("tool.output.delta", "call-tail", "stdout", "first", 1),
            EventCursor(1, 1),
        )
        timeline.resetReadCount()

        val second = reducer.apply(
            first,
            eventWithSequence("tool.output.delta", "call-tail", "stdout", " second", 2),
            EventCursor(1, 2),
        )

        assertEquals(
            "first second",
            (second.timeline.last() as SessionTimelineItem.ToolActivity).output,
        )
        assertTrue(
            timeline.readCount <= 8,
            "Reducer read ${timeline.readCount} settled live timeline items for one tool delta.",
        )
    }

    @Test
    fun `assistant stream events create one turn when message start is absent`() {
        var state = seed(connectionEpoch = 1)

        state = reducer.apply(
            state,
            event("message.delta", "text" to "partial"),
            EventCursor(1, 1),
        )
        state = reducer.apply(
            state,
            event("reasoning.delta", "text" to "reason"),
            EventCursor(1, 2),
        )
        state = reducer.apply(
            state,
            event("thinking.delta", "text" to "thought"),
            EventCursor(1, 3),
        )
        state = reducer.apply(
            state,
            event("status.update", "text" to "working"),
            EventCursor(1, 4),
        )

        val segments = state.timeline.filterIsInstance<SessionTimelineItem.AssistantTurn>()
        assertEquals(
            listOf(
                AssistantSegmentKind.RESPONSE,
                AssistantSegmentKind.REASONING,
                AssistantSegmentKind.THINKING,
                AssistantSegmentKind.ACTIVITY,
            ),
            segments.map { it.segmentKind },
        )
        assertEquals(listOf("assistant:1:1"), segments.map { it.turnKey }.distinct())
        assertEquals("partial", segments[0].text)
        assertEquals("reason", segments[1].reasoning)
        assertEquals("thought", segments[2].thinking)
        assertEquals("working", segments[3].statusText)
    }

    @Test
    fun `interim already streamed does not duplicate text or create another final turn`() {
        var state = seed(connectionEpoch = 1)

        state = reducer.apply(state, event("message.start"), EventCursor(1, 1))
        state = reducer.apply(state, event("message.delta", "text" to "hello"), EventCursor(1, 2))
        state = reducer.apply(
            state,
            GatewayEvent(
                type = "message.interim",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("text", "hello")
                    put("already_streamed", true)
                },
            ),
            EventCursor(1, 3),
        )
        state = reducer.apply(state, event("message.complete", "text" to "hello"), EventCursor(1, 4))

        val turn = assertIs<SessionTimelineItem.AssistantTurn>(state.timeline.single())
        assertEquals("hello", turn.text)
        assertEquals(AssistantTurnStatus.COMPLETE, turn.status)
        assertEquals(1, state.liveMessages.size)
    }

    @Test
    fun `complete full text replaces a divergent partial stream`() {
        var state = seed(connectionEpoch = 1)

        state = reducer.apply(state, event("message.start"), EventCursor(1, 1))
        state = reducer.apply(
            state,
            event("message.delta", "text" to "stale partial"),
            EventCursor(1, 2),
        )
        state = reducer.apply(
            state,
            event("message.complete", "text" to "authoritative final"),
            EventCursor(1, 3),
        )

        val turn = assertIs<SessionTimelineItem.AssistantTurn>(state.timeline.single())
        assertEquals("authoritative final", turn.text)
        assertEquals(AssistantTurnStatus.COMPLETE, turn.status)
    }

    @Test
    fun `stream deltas are merged once and completion seals the assistant message`() {
        var state = seed(connectionEpoch = 1)

        state = reducer.apply(state, event("message.start"), EventCursor(1, 1))
        state = reducer.apply(state, event("message.delta", "text" to "hel"), EventCursor(1, 2))
        state = reducer.apply(state, event("message.delta", "text" to "lo"), EventCursor(1, 3))
        state = reducer.apply(state, event("message.delta", "text" to "lo"), EventCursor(1, 3))
        state = reducer.apply(state, event("reasoning.delta", "text" to "checked"), EventCursor(1, 4))
        state = reducer.apply(state, event("message.complete", "text" to "hello"), EventCursor(1, 5))

        assertFalse(state.running)
        assertEquals("", state.streamingAssistantText)
        assertEquals("hello", state.liveMessages.single().text)
        assertEquals("checked", state.liveMessages.single().reasoning)
        assertEquals(LiveMessageStatus.COMPLETE, state.liveMessages.single().status)
    }

    @Test
    fun `reasoning thinking and status stay in separate assistant fields with newlines`() {
        var state = seed(connectionEpoch = 1)

        state = reducer.apply(state, event("message.start"), EventCursor(1, 1))
        state = reducer.apply(
            state,
            event("reasoning.delta", "text" to "reason one\n"),
            EventCursor(1, 2),
        )
        state = reducer.apply(
            state,
            event("reasoning.delta", "text" to "reason two"),
            EventCursor(1, 3),
        )
        state = reducer.apply(
            state,
            event("thinking.delta", "text" to "thinking\nline"),
            EventCursor(1, 4),
        )
        state = reducer.apply(
            state,
            event("status.update", "kind" to "working", "text" to "searching\n"),
            EventCursor(1, 5),
        )

        val segments = state.timeline.filterIsInstance<SessionTimelineItem.AssistantTurn>()
        assertEquals(
            listOf(
                AssistantSegmentKind.REASONING,
                AssistantSegmentKind.THINKING,
                AssistantSegmentKind.ACTIVITY,
            ),
            segments.map { it.segmentKind },
        )
        assertEquals("reason one\nreason two", segments[0].reasoning)
        assertEquals("thinking\nline", segments[1].thinking)
        assertEquals("searching\n", segments[2].statusText)
        assertTrue(segments.all { it.text.isEmpty() })
    }

    @Test
    fun `foreign runtime events and events before REST resync are ignored`() {
        val initial = seed(connectionEpoch = 1)
        val foreign = GatewayEvent(
            type = "message.delta",
            runtimeSessionId = RuntimeSessionId("runtime-2"),
            payload = buildJsonObject { put("text", "wrong session") },
        )

        val afterForeign = reducer.apply(initial, foreign, EventCursor(1, 1))
        val beforeResync = reducer.apply(initial, event("message.delta", "text" to "new socket"), EventCursor(2, 1))

        assertEquals(initial, afterForeign)
        assertEquals(initial, beforeResync)

        val resynced = reducer.resync(initial, transcript(), connectionEpoch = 2)
        val accepted = reducer.apply(resynced, event("message.delta", "text" to "new socket"), EventCursor(2, 1))
        assertEquals("new socket", accepted.streamingAssistantText)
    }

    @Test
    fun `tool events merge by tool id and output sequence without erasing earlier fields`() {
        var state = seed(connectionEpoch = 4)

        state = reducer.apply(state, event("message.start"), EventCursor(4, 1))
        state = reducer.apply(
            state,
            event(
                "tool.start",
                "tool_id" to "call-1",
                "name" to "terminal",
                "context" to "workspace",
                "args_text" to "pwd",
            ),
            EventCursor(4, 2),
        )
        state = reducer.apply(
            state,
            event("tool.progress", "tool_id" to "call-1", "summary" to "running"),
            EventCursor(4, 3),
        )
        state = reducer.apply(
            state,
            eventWithSequence("tool.output.delta", "call-1", "stdout", "one\n", 1),
            EventCursor(4, 4),
        )
        state = reducer.apply(
            state,
            eventWithSequence("tool.output.delta", "call-1", "stdout", "one\n", 1),
            EventCursor(4, 5),
        )
        state = reducer.apply(
            state,
            eventWithSequence("tool.output.delta", "call-1", "stderr", "two", 2),
            EventCursor(4, 6),
        )
        state = reducer.apply(
            state,
            GatewayEvent(
                type = "tool.complete",
                runtimeSessionId = runtimeId,
                payload = buildJsonObject {
                    put("tool_id", "call-1")
                    put("name", "terminal")
                    put("result_text", "/workspace")
                    put("summary", "done")
                    put("inline_diff", "diff text")
                    put("duration_s", 1.25)
                },
            ),
            EventCursor(4, 7),
        )
        state = reducer.apply(
            state,
            event("tool.complete", "tool_id" to "call-1"),
            EventCursor(4, 8),
        )

        assertEquals(3, state.timeline.size)
        val tool = state.timeline.filterIsInstance<SessionTimelineItem.ToolActivity>().single()
        val resultMarker = state.timeline.filterIsInstance<SessionTimelineItem.ToolResultActivity>().single()
        assertEquals("tool:call-1:result", resultMarker.key)
        assertEquals("tool:call-1", resultMarker.toolKey)
        assertEquals("tool:call-1", tool.key)
        assertEquals("call-1", tool.toolId)
        assertEquals("terminal", tool.name)
        assertEquals("workspace", tool.context)
        assertEquals("pwd", tool.args)
        assertEquals("one\ntwo", tool.output)
        assertEquals("/workspace", tool.result)
        assertEquals("done", tool.summary)
        assertEquals("diff text", tool.diff)
        assertEquals(1.25, tool.durationSeconds)
        assertEquals(ToolActivityStatus.COMPLETE, tool.status)

        assertEquals("call-1", state.tools.single().key)
        assertEquals("terminal", state.tools.single().name)
        assertEquals(LiveToolStatus.COMPLETE, state.tools.single().status)
        assertEquals("workspace", state.tools.single().payload["context"]?.toString()?.trim('"'))
    }

    @Test
    fun `tool payload output is retained before later output deltas`() {
        var state = seed(connectionEpoch = 4)

        state = reducer.apply(
            state,
            event(
                "tool.start",
                "tool_id" to "call-output",
                "name" to "terminal",
                "output" to "seed\n",
            ),
            EventCursor(4, 1),
        )
        state = reducer.apply(
            state,
            eventWithSequence(
                "tool.output.delta",
                "call-output",
                "stdout",
                "tail",
                1,
            ),
            EventCursor(4, 2),
        )

        val tool = assertIs<SessionTimelineItem.ToolActivity>(state.timeline.single())
        assertEquals("seed\ntail", tool.output)
        assertEquals(
            "seed\ntail",
            tool.payload["output"]?.toString()?.trim('"')?.replace("\\n", "\n"),
        )
    }

    @Test
    fun `tool output and sequence deduplication stay bounded`() {
        var state = seed(connectionEpoch = 4)
        state = reducer.apply(
            state,
            event("message.start"),
            EventCursor(4, 1),
        )
        state = reducer.apply(
            state,
            eventWithSequence(
                "tool.output.delta",
                "bounded-tool",
                "stdout",
                "😀".repeat(MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS + 64),
                1,
            ),
            EventCursor(4, 2),
        )
        state = reducer.apply(
            state,
            eventWithSequence(
                "tool.output.delta",
                "bounded-tool",
                "stdout",
                "must not grow retained output",
                2,
            ),
            EventCursor(4, 3),
        )

        val output = assertIs<SessionTimelineItem.ToolActivity>(state.timeline.last()).output
        assertTrue(
            output.codePointCount(0, output.length) <= MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS,
            output.length.toString(),
        )
        assertTrue(output.endsWith(REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER), output.takeLast(80))
        assertFalse(output.contains("must not grow retained output"))
        val payloadOutput = assertIs<JsonPrimitive>(
            state.tools.single().payload["output"],
        ).content
        assertEquals(output, payloadOutput)
        assertNull(state.tools.single().payload["output_text"])

        state = state.copy(
            seenToolOutputSequences = mapOf(
                "bounded-tool" to (1L..MAX_TRACKED_TOOL_OUTPUT_SEQUENCES.toLong()).toSet(),
            ),
        )
        state = reducer.apply(
            state,
            eventWithSequence(
                "tool.output.delta",
                "bounded-tool",
                "stdout",
                "",
                MAX_TRACKED_TOOL_OUTPUT_SEQUENCES + 1L,
            ),
            EventCursor(4, 4),
        )

        val tracked = state.seenToolOutputSequences.getValue("bounded-tool")
        assertEquals(MAX_TRACKED_TOOL_OUTPUT_SEQUENCES, tracked.size)
        assertFalse(1L in tracked)
        assertTrue(MAX_TRACKED_TOOL_OUTPUT_SEQUENCES + 1L in tracked)
    }

    @Test
    fun `first output delta does not duplicate an output field when start is absent`() {
        var state = seed(connectionEpoch = 4)

        state = reducer.apply(
            state,
            event(
                "tool.output.delta",
                "tool_id" to "call-output-only",
                "name" to "terminal",
                "output" to "single chunk",
            ),
            EventCursor(4, 1),
        )

        val tool = assertIs<SessionTimelineItem.ToolActivity>(state.timeline.single())
        assertEquals("single chunk", tool.output)
    }

    @Test
    fun `output text delta alias is normalized without retaining a duplicate field`() {
        var state = seed(connectionEpoch = 4)

        state = reducer.apply(
            state,
            event(
                "tool.output.delta",
                "tool_id" to "call-output-text",
                "name" to "terminal",
                "output_text" to "single alias chunk",
            ),
            EventCursor(4, 1),
        )

        val tool = assertIs<SessionTimelineItem.ToolActivity>(state.timeline.single())
        assertEquals("single alias chunk", tool.output)
        assertEquals("single alias chunk", tool.payload["output"]?.toString()?.trim('"'))
        assertNull(tool.payload["output_text"])
    }

    @Test
    fun `background terminal chunks append in real time and completion closes activity`() {
        var state = seed(connectionEpoch = 4)

        state = reducer.apply(
            state,
            event(
                "agent.terminal.output",
                "process_id" to "process-1",
                "chunk" to "first\n",
            ),
            EventCursor(4, 1),
        )
        state = reducer.apply(
            state,
            event(
                "agent.terminal.output",
                "process_id" to "process-1",
                "chunk" to "second",
            ),
            EventCursor(4, 2),
        )

        val running = assertIs<SessionTimelineItem.ToolActivity>(state.timeline.single())
        assertEquals("process:process-1", running.toolId)
        assertEquals("terminal", running.name)
        assertEquals("first\nsecond", running.output)
        assertEquals(ToolActivityStatus.RUNNING, running.status)

        state = reducer.apply(
            state,
            event(
                "agent.terminal.complete",
                "process_id" to "process-1",
                "exit_code" to "0",
            ),
            EventCursor(4, 3),
        )

        val complete = state.timeline.filterIsInstance<SessionTimelineItem.ToolActivity>().single()
        assertEquals(1, state.timeline.filterIsInstance<SessionTimelineItem.ToolResultActivity>().size)
        assertEquals("first\nsecond", complete.output)
        assertEquals(ToolActivityStatus.COMPLETE, complete.status)
    }

    @Test
    fun `background terminal nonzero exit keeps output and marks activity failed`() {
        var state = seed(connectionEpoch = 4)
        state = reducer.apply(
            state,
            event(
                "agent.terminal.output",
                "process_id" to "process-1",
                "chunk" to "failure detail",
            ),
            EventCursor(4, 1),
        )

        state = reducer.apply(
            state,
            event(
                "agent.terminal.complete",
                "process_id" to "process-1",
                "exit_code" to "7",
            ),
            EventCursor(4, 2),
        )

        val failed = state.timeline.filterIsInstance<SessionTimelineItem.ToolActivity>().single()
        assertEquals(1, state.timeline.filterIsInstance<SessionTimelineItem.ToolResultActivity>().size)
        assertEquals("failure detail", failed.output)
        assertEquals(ToolActivityStatus.ERROR, failed.status)
        assertEquals("Process exited with code 7.", failed.error)
    }

    @Test
    fun `terminal completion without an exit code stays explicitly unknown`() {
        var state = seed(connectionEpoch = 4)
        state = reducer.apply(
            state,
            event(
                "agent.terminal.output",
                "process_id" to "process-unknown",
                "chunk" to "partial output",
            ),
            EventCursor(4, 1),
        )

        state = reducer.apply(
            state,
            event(
                "agent.terminal.complete",
                "process_id" to "process-unknown",
            ),
            EventCursor(4, 2),
        )

        val unknown = state.timeline.filterIsInstance<SessionTimelineItem.ToolActivity>().single()
        assertEquals("partial output", unknown.output)
        assertEquals("UNKNOWN", unknown.status.name)
        assertEquals("UNKNOWN", state.tools.single().status.name)
        assertNull(unknown.error)
    }

    @Test
    fun `observer ignores interactive pending input payloads while preserving tool lifecycle`() {
        var state = seed(connectionEpoch = 4)

        state = reducer.apply(
            state,
            event("tool.start", "id" to "call-1", "name" to "terminal"),
            EventCursor(4, 1),
        )
        listOf("approval", "clarify", "secret", "sudo").forEachIndexed { index, kind ->
            state = reducer.apply(
                state,
                event("$kind.request", "request_id" to "$kind-1", "secret" to "must-not-store"),
                EventCursor(4, (index + 2).toLong()),
            )
        }
        state = reducer.apply(
            state,
            event("tool.complete", "id" to "call-1", "name" to "terminal"),
            EventCursor(4, 6),
        )

        assertEquals(LiveToolStatus.COMPLETE, state.tools.single().status)
        assertNull(state.pendingInput)

        state = reducer.apply(state, event("approval.expire"), EventCursor(4, 7))
        assertNull(state.pendingInput)
    }

    @Test
    fun `subagent completion and moa reference update stable process projections`() {
        var state = seed(connectionEpoch = 7)
        state = reducer.apply(state, event("message.start"), EventCursor(7, 1))
        state = reducer.apply(
            state,
            event(
                "subagent.start",
                "subagent_id" to "child-1",
                "parent_id" to "parent-1",
                "goal" to "Inspect the projector",
                "model" to "test-model",
                "task_index" to "0",
                "task_count" to "2",
            ),
            EventCursor(7, 2),
        )
        state = reducer.apply(
            state,
            event(
                "subagent.complete",
                "subagent_id" to "child-1",
                "status" to "completed",
                "summary" to "Projector inspected",
                "duration_seconds" to "1.5",
                "api_calls" to "3",
            ),
            EventCursor(7, 3),
        )
        state = reducer.apply(
            state,
            event(
                "moa.reference",
                "label" to "Advisor 1",
                "text" to "Reference answer",
                "index" to "0",
                "count" to "2",
            ),
            EventCursor(7, 4),
        )
        state = reducer.apply(
            state,
            event(
                "moa.progress",
                "label" to "Advisor 1",
                "refs_done" to "1",
                "refs_total" to "2",
            ),
            EventCursor(7, 5),
        )
        state = reducer.apply(
            state,
            event(
                "moa.phase",
                "phase" to "aggregator",
                "aggregator" to "test-model",
                "refs_done" to "2",
                "refs_total" to "2",
            ),
            EventCursor(7, 6),
        )

        assertEquals(1, state.subagents.size)
        assertEquals(
            LiveSubagentProjection(
                key = "assistant:7:1:subagent:child-1",
                turnKey = "assistant:7:1",
                parentKey = "assistant:7:1:subagent:parent-1",
                goal = "Inspect the projector",
                model = "test-model",
                status = LiveSubagentStatus.COMPLETE,
                summary = "Projector inspected",
                durationSeconds = 1.5,
                taskIndex = 0,
                taskCount = 2,
                apiCalls = 3,
            ),
            state.subagents.single(),
        )
        assertEquals(
            listOf(
                LiveMoaReferenceProjection(
                    key = "assistant:7:1:moa:0",
                    turnKey = "assistant:7:1",
                    label = "Advisor 1",
                    text = "Reference answer",
                    index = 0,
                    count = 2,
                ),
            ),
            state.moaReferences,
        )
        assertEquals(
            LiveMoaProgressProjection(
                turnKey = "assistant:7:1",
                phase = "aggregator",
                aggregator = "test-model",
                refsDone = 2,
                refsTotal = 2,
            ),
            state.moaProgress.single(),
        )
    }

    @Test
    fun `subagent and moa presentation fields are sanitized before projection storage`() {
        var state = seed(connectionEpoch = 8)
        state = reducer.apply(state, event("message.start"), EventCursor(8, 1))
        state = reducer.apply(
            state,
            event(
                "subagent.start",
                "subagent_id" to "child-1",
                "goal" to "ticket=goal-secret",
                "model" to "ws_ticket=model-secret",
            ),
            EventCursor(8, 2),
        )
        state = reducer.apply(
            state,
            event(
                "subagent.complete",
                "subagent_id" to "child-1",
                "summary" to "lease_id=summary-secret",
            ),
            EventCursor(8, 3),
        )
        state = reducer.apply(
            state,
            event(
                "moa.reference",
                "label" to "ticket=label-secret",
                "text" to "control_lease_id=text-secret",
                "index" to "0",
            ),
            EventCursor(8, 4),
        )
        state = reducer.apply(
            state,
            event(
                "moa.phase",
                "phase" to "ws_ticket=phase-secret",
                "aggregator" to "lease_id=aggregator-secret",
            ),
            EventCursor(8, 5),
        )

        val visible = buildList {
            state.subagents.single().let { subagent ->
                add(subagent.goal)
                add(subagent.model.orEmpty())
                add(subagent.summary.orEmpty())
            }
            state.moaReferences.single().let { reference ->
                add(reference.label)
                add(reference.text)
            }
            state.moaProgress.single().let { progress ->
                add(progress.phase.orEmpty())
                add(progress.aggregator.orEmpty())
            }
        }.joinToString("\n")

        assertTrue(visible.contains("[redacted]"))
        listOf(
            "goal-secret",
            "model-secret",
            "summary-secret",
            "label-secret",
            "text-secret",
            "phase-secret",
            "aggregator-secret",
        ).forEach { secret -> assertFalse(visible.contains(secret)) }
    }

    @Test
    fun `terminal error is explicit and never marks an unconfirmed prompt delivered`() {
        var state = seed(connectionEpoch = 1)
        state = reducer.markPromptQueued(state, clientPromptId = "client-1", text = "hello")

        assertEquals(PromptDeliveryState.QUEUED_LOCALLY, state.outboundPrompts.single().deliveryState)

        state = reducer.markPromptAccepted(state, "client-1")
        state = reducer.apply(
            state,
            event("message.complete", "status" to "error", "error" to "model unavailable"),
            EventCursor(1, 1),
        )

        assertEquals(PromptDeliveryState.ACCEPTED_BY_GATEWAY, state.outboundPrompts.single().deliveryState)
        assertEquals(LiveMessageStatus.ERROR, state.liveMessages.single().status)
        assertTrue(state.lastError?.contains("model unavailable") == true)
    }

    @Test
    fun `realtime error preserves partial assistant text and seals the turn as failed`() {
        var state = seed(connectionEpoch = 1)
        state = reducer.apply(state, event("message.start"), EventCursor(1, 1))
        state = reducer.apply(
            state,
            event("message.delta", "text" to "partial answer"),
            EventCursor(1, 2),
        )

        state = reducer.apply(
            state,
            event("error", "message" to "connection failed"),
            EventCursor(1, 3),
        )

        val failed = assertIs<SessionTimelineItem.AssistantTurn>(state.timeline.single())
        assertEquals("partial answer", failed.text)
        assertEquals(AssistantTurnStatus.ERROR, failed.status)
        assertEquals("connection failed", failed.error)
        assertEquals(LiveMessageStatus.ERROR, state.liveMessages.single().status)
        assertEquals("partial answer", state.liveMessages.single().text)
        assertEquals("connection failed", state.lastError)
        assertNull(state.activeAssistantTurnKey)
        assertFalse(state.running)
    }

    private class TimelineReadCountingList(
        private val values: List<SessionTimelineItem>,
    ) : AbstractList<SessionTimelineItem>() {
        var readCount: Int = 0
            private set

        override val size: Int
            get() = values.size

        override fun get(index: Int): SessionTimelineItem {
            readCount += 1
            return values[index]
        }

        fun resetReadCount() {
            readCount = 0
        }
    }

    private fun seed(connectionEpoch: Long): RealtimeSessionProjection =
        reducer.seed(transcript(), runtimeId, connectionEpoch)

    private fun transcript() = SessionTranscript(
        sessionKey = sessionKey,
        lineageTip = sessionKey,
        messages = emptyList(),
        pagination = app.hermesmobile.protocol.sessions.TranscriptPagination(
            limit = 200,
            offset = 0,
            returned = 0,
        ),
    )

    private fun event(type: String, vararg fields: Pair<String, String>): GatewayEvent =
        GatewayEvent(
            type = type,
            runtimeSessionId = runtimeId,
            payload = buildJsonObject {
                fields.forEach { (key, value) -> put(key, value) }
            },
        )

    private fun v2Event(type: String, sequence: Long, payload: String): GatewayEvent =
        GatewayEvent(
            type = type,
            runtimeSessionId = runtimeId,
            payload = Json.parseToJsonElement(payload).jsonObject,
            sessionKey = sessionKey,
            eventSequence = sequence,
        )

    private fun eventWithSequence(
        type: String,
        toolId: String,
        stream: String,
        text: String,
        sequence: Long,
    ): GatewayEvent = GatewayEvent(
        type = type,
        runtimeSessionId = runtimeId,
        payload = buildJsonObject {
            put("tool_id", toolId)
            put("stream", stream)
            put("text", text)
            put("sequence", sequence)
        },
    )
}
