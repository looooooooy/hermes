package app.hermesmobile.sessions

import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import kotlinx.serialization.json.JsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotSame
import kotlin.test.assertSame

class ConversationTurnProjectionCacheTest {
    @Test
    fun `successive realtime updates reuse one historical baseline`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var baselineCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = { source ->
                baselineCalls += 1
                projector.projectBaseline(source)
            },
            projectRealtime = projector::project,
        )
        val firstRealtime = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        )

        cache.project(transcript, firstRealtime)
        cache.project(transcript, firstRealtime.copy(running = true))

        assertEquals(1, baselineCalls)
    }

    @Test
    fun `identical projection inputs bypass duplicate realtime projection`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var realtimeCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                realtimeCalls += 1
                projector.project(baseline, realtime)
            },
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        )

        val first = cache.project(transcript, realtime)
        val repeated = cache.project(transcript, realtime)

        assertEquals(1, realtimeCalls)
        assertSame(first, repeated)
    }

    @Test
    fun `aligned realtime delta does not build a keyed index over stable history`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        val historical = projectedTurn(key = "turn-1", response = "Complete")
        val streaming = projectedTurn(key = "turn-2", response = "Streaming")
        val previous = IteratorCountingList(listOf(historical, streaming))
        var realtimeCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { _, _ ->
                realtimeCalls += 1
                if (realtimeCalls == 1) {
                    previous
                } else {
                    listOf(historical, streaming.copy(response = "Streaming answer"))
                }
            },
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        )

        val beforeDelta = cache.project(transcript, realtime)
        val afterDelta = cache.project(transcript, realtime.copy(running = true))

        assertEquals(0, previous.iteratorCalls)
        assertSame(beforeDelta.first(), afterDelta.first())
        assertNotSame(beforeDelta.last(), afterDelta.last())
    }

    @Test
    fun `inserted turn falls back to stable key reuse`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        val first = projectedTurn(key = "turn-1", response = "First")
        val second = projectedTurn(key = "turn-2", response = "Second")
        var realtimeCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { _, _ ->
                realtimeCalls += 1
                if (realtimeCalls == 1) {
                    listOf(first, second)
                } else {
                    listOf(
                        projectedTurn(key = "turn-new", response = "Inserted"),
                        first.copy(),
                        second.copy(response = "Second updated"),
                    )
                }
            },
        )
        val realtime = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        )

        val beforeInsertion = cache.project(transcript, realtime)
        val afterInsertion = cache.project(transcript, realtime.copy(running = true))

        assertSame(beforeInsertion[0], afterInsertion[1])
        assertNotSame(beforeInsertion[1], afterInsertion[2])
        assertEquals("Second updated", afterInsertion[2].response)
    }

    @Test
    fun `later streaming deltas retain unchanged live turn instances`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = projector::project,
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-1"),
            connectionEpoch = 1,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "First"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "Complete answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.User(key = "user-2", text = "Second"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-2",
                    text = "Streaming",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )

        val beforeDelta = cache.project(transcript, initial)
        val afterDelta = cache.project(
            transcript,
            initial.copy(
                timeline = initial.timeline.map { item ->
                    if (item is SessionTimelineItem.AssistantTurn && item.key == "assistant-2") {
                        item.copy(text = "Streaming answer")
                    } else {
                        item
                    }
                },
            ),
        )

        assertSame(beforeDelta.first(), afterDelta.first())
        assertNotSame(beforeDelta.last(), afterDelta.last())
        assertEquals("Streaming answer", afterDelta.last().response)
    }

    @Test
    fun `tool output delta rebuilds and visits only its containing logical turn`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-tool-output"),
            connectionEpoch = 5,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "Run the tool"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "Running it",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.ToolActivity(
                    key = "tool-1",
                    toolId = "tool-1",
                    name = "terminal",
                    output = "partial",
                    status = ToolActivityStatus.RUNNING,
                ),
                SessionTimelineItem.ToolResultActivity(
                    key = "tool-result-1",
                    toolKey = "tool-1",
                ),
                SessionTimelineItem.User(key = "user-2", text = "Next question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-2",
                    text = "Streaming next answer",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )
        val beforeDelta = cache.project(transcript, initial)
        incrementallyVisitedKeys.clear()

        val afterDelta = cache.project(
            transcript,
            initial.copy(
                timeline = initial.timeline.map { item ->
                    if (item is SessionTimelineItem.ToolActivity && item.key == "tool-1") {
                        item.copy(output = "partial\ncomplete")
                    } else {
                        item
                    }
                },
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(
            listOf("user-1", "assistant-1", "tool-1", "tool-result-1"),
            incrementallyVisitedKeys,
        )
        assertNotSame(beforeDelta[0], afterDelta[0])
        assertSame(beforeDelta[1], afterDelta[1])
        assertEquals("partial\ncomplete", afterDelta[0].tools.single().output)
    }

    @Test
    fun `external subagent and moa changes rebuild only their owning logical turn`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-external-process"),
            connectionEpoch = 5,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "First"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "First answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.User(key = "user-2", text = "Second"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-2",
                    text = "Second answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
            ),
        )
        val beforeProcess = cache.project(transcript, initial)
        incrementallyVisitedKeys.clear()

        val withProcess = initial.copy(
            subagents = listOf(
                LiveSubagentProjection(
                    key = "assistant-1:subagent:child",
                    turnKey = "assistant-1",
                    goal = "Inspect",
                    status = LiveSubagentStatus.RUNNING,
                ),
            ),
            moaReferences = listOf(
                LiveMoaReferenceProjection(
                    key = "assistant-1:moa:0",
                    turnKey = "assistant-1",
                    label = "Advisor",
                    text = "Reference",
                ),
            ),
            moaProgress = listOf(
                LiveMoaProgressProjection(
                    turnKey = "assistant-1",
                    phase = "references",
                    refsDone = 1,
                    refsTotal = 2,
                ),
            ),
        )
        val afterProcess = cache.project(transcript, withProcess)

        assertEquals(1, fullProjectionCalls)
        assertEquals(listOf("user-1", "assistant-1"), incrementallyVisitedKeys)
        assertNotSame(beforeProcess[0], afterProcess[0])
        assertSame(beforeProcess[1], afterProcess[1])
        val processSection = afterProcess[0].sections
            .filterIsInstance<HermesConversationSection.Subagents>()
            .single()
        assertEquals(1, processSection.subagents.size)
        assertEquals(1, processSection.moaReferences.size)
        assertEquals("references", processSection.moaProgress?.phase)

        incrementallyVisitedKeys.clear()
        val afterUpdate = cache.project(
            transcript,
            withProcess.copy(
                subagents = withProcess.subagents.map { subagent ->
                    subagent.copy(
                        status = LiveSubagentStatus.COMPLETE,
                        summary = "Inspected",
                    )
                },
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(listOf("user-1", "assistant-1"), incrementallyVisitedKeys)
        assertNotSame(afterProcess[0], afterUpdate[0])
        assertSame(afterProcess[1], afterUpdate[1])

        incrementallyVisitedKeys.clear()
        val reassignedProcess = withProcess.copy(
            subagents = withProcess.subagents.map { subagent ->
                subagent.copy(turnKey = "assistant-2")
            },
        )
        val afterReassignment = cache.project(
            transcript,
            reassignedProcess,
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(
            listOf("user-1", "assistant-1", "user-2", "assistant-2"),
            incrementallyVisitedKeys,
        )
        assertNotSame(afterUpdate[0], afterReassignment[0])
        assertNotSame(afterUpdate[1], afterReassignment[1])

        incrementallyVisitedKeys.clear()
        val afterRemoval = cache.project(
            transcript,
            reassignedProcess.copy(
                subagents = emptyList(),
                moaReferences = emptyList(),
                moaProgress = emptyList(),
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(
            listOf("user-1", "assistant-1", "user-2", "assistant-2"),
            incrementallyVisitedKeys,
        )
        assertNotSame(afterReassignment[0], afterRemoval[0])
        assertNotSame(afterReassignment[1], afterRemoval[1])
        assertEquals(
            emptyList(),
            afterRemoval[0].sections.filterIsInstance<HermesConversationSection.Subagents>(),
        )
    }

    @Test
    fun `appending a new logical turn retains completed live turns without full projection`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-append"),
            connectionEpoch = 6,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "First question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "First answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.User(key = "user-2", text = "Second question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-2",
                    text = "Second answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
            ),
        )
        val beforeAppend = cache.project(transcript, initial)
        incrementallyVisitedKeys.clear()

        val afterAppend = cache.project(
            transcript,
            initial.copy(
                running = true,
                timeline = initial.timeline + listOf(
                    SessionTimelineItem.User(key = "user-3", text = "Third question"),
                    SessionTimelineItem.AssistantTurn(
                        key = "assistant-3",
                        text = "Third answer streaming",
                        status = AssistantTurnStatus.STREAMING,
                    ),
                ),
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(listOf("user-3", "assistant-3"), incrementallyVisitedKeys)
        assertSame(beforeAppend[0], afterAppend[0])
        assertSame(beforeAppend[1], afterAppend[1])
        assertEquals("Third question", afterAppend[2].userPrompt?.text)
        assertEquals("Third answer streaming", afterAppend[2].response)
    }

    @Test
    fun `appending first assistant to pending user rebuilds only that turn`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val pending = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-pending-user"),
            connectionEpoch = 6,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.User(key = "user-pending", text = "Question"),
            ),
        )
        val beforeAssistant = cache.project(transcript, pending)
        incrementallyVisitedKeys.clear()

        val afterAssistant = cache.project(
            transcript,
            pending.copy(
                activeAssistantTurnKey = "assistant-pending",
                timeline = pending.timeline + SessionTimelineItem.AssistantTurn(
                    key = "assistant-pending",
                    text = "Streaming answer",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(listOf("user-pending", "assistant-pending"), incrementallyVisitedKeys)
        assertNotSame(beforeAssistant.single(), afterAssistant.single())
        assertEquals("Question", afterAssistant.single().userPrompt?.text)
        assertEquals("Streaming answer", afterAssistant.single().response)
    }

    @Test
    fun `runtime identity change discards prior turn instances after full fallback`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-before"),
            connectionEpoch = 7,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "Question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "Answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
            ),
        )
        val beforeRuntimeChange = cache.project(transcript, initial)

        val afterRuntimeChange = cache.project(
            transcript,
            initial.copy(runtimeSessionId = RuntimeSessionId("runtime-after")),
        )

        assertEquals(2, fullProjectionCalls)
        assertEquals(emptyList(), incrementallyVisitedKeys)
        assertNotSame(beforeRuntimeChange, afterRuntimeChange)
        assertNotSame(beforeRuntimeChange.single(), afterRuntimeChange.single())
    }

    @Test
    fun `connection epoch change discards prior turn instances after full fallback`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-epoch"),
            connectionEpoch = 7,
        ).copy(
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "Question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "Answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
            ),
        )
        val beforeEpochChange = cache.project(transcript, initial)

        val afterEpochChange = cache.project(
            transcript,
            initial.copy(connectionEpoch = 8),
        )

        assertEquals(2, fullProjectionCalls)
        assertEquals(emptyList(), incrementallyVisitedKeys)
        assertNotSame(beforeEpochChange, afterEpochChange)
        assertNotSame(beforeEpochChange.single(), afterEpochChange.single())
    }

    @Test
    fun `new authoritative transcript identity never reuses same-key turn instances`() {
        val originalTranscript = transcriptWithCompletedTurns(1)
        val replacementTranscript = originalTranscript.copy(
            messages = originalTranscript.messages.map { message ->
                if (message.role == "assistant") {
                    message.copy(content = JsonPrimitive("Replacement authoritative answer"))
                } else {
                    message
                }
            },
        )
        val projector = ConversationTurnProjector()
        var baselineCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = { transcript ->
                baselineCalls += 1
                projector.projectBaseline(transcript)
            },
            projectRealtime = projector::project,
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
        )

        val original = cache.project(originalTranscript, realtime = null)
        val replacement = cache.project(replacementTranscript, realtime = null)

        assertEquals(2, baselineCalls)
        assertNotSame(original, replacement)
        assertNotSame(original.single(), replacement.single())
        assertEquals("Replacement authoritative answer", replacement.single().response)
    }

    @Test
    fun `timeline reorder falls back once then recovers incremental projection`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val firstTurn = listOf(
            SessionTimelineItem.User(key = "user-1", text = "First question"),
            SessionTimelineItem.AssistantTurn(
                key = "assistant-1",
                text = "First answer",
                status = AssistantTurnStatus.COMPLETE,
            ),
        )
        val secondTurn = listOf(
            SessionTimelineItem.User(key = "user-2", text = "Second question"),
            SessionTimelineItem.AssistantTurn(
                key = "assistant-2",
                text = "Second answer",
                status = AssistantTurnStatus.STREAMING,
            ),
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-reorder"),
            connectionEpoch = 3,
        ).copy(running = true, timeline = firstTurn + secondTurn)
        cache.project(transcript, initial)

        val reordered = initial.copy(timeline = secondTurn + firstTurn)
        val afterReorder = cache.project(transcript, reordered)

        assertEquals(2, fullProjectionCalls)
        assertEquals(emptyList(), incrementallyVisitedKeys)
        assertEquals(listOf("Second question", "First question"), afterReorder.map { it.userPrompt?.text })

        incrementallyVisitedKeys.clear()
        val afterNextDelta = cache.project(
            transcript,
            reordered.copy(
                timeline = reordered.timeline.map { item ->
                    if (item is SessionTimelineItem.AssistantTurn && item.key == "assistant-2") {
                        item.copy(text = "Second answer continues")
                    } else {
                        item
                    }
                },
            ),
        )

        assertEquals(2, fullProjectionCalls)
        assertEquals(listOf("user-2", "assistant-2"), incrementallyVisitedKeys)
        assertEquals("Second answer continues", afterNextDelta.first().response)
        assertSame(afterReorder.last(), afterNextDelta.last())
    }

    @Test
    fun `token delta materializes only the active logical tail after large settled history`() {
        val transcript = transcriptWithCompletedTurns(150)
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val incrementallyVisitedKeys = mutableListOf<String>()
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
            onIncrementalTimelineItemVisit = { item ->
                incrementallyVisitedKeys += item.key
            },
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-active-tail"),
            connectionEpoch = 4,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.User(key = "user-1", text = "First live question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-1",
                    text = "First live answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.User(key = "user-2", text = "Second live question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-2",
                    text = "Second live answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.User(key = "user-3", text = "Active question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-3",
                    text = "Streaming",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )
        val beforeDelta = cache.project(transcript, initial)
        incrementallyVisitedKeys.clear()

        val afterDelta = cache.project(
            transcript,
            initial.copy(
                timeline = initial.timeline.map { item ->
                    if (item is SessionTimelineItem.AssistantTurn && item.key == "assistant-3") {
                        item.copy(text = "Streaming answer")
                    } else {
                        item
                    }
                },
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertEquals(listOf("user-3", "assistant-3"), incrementallyVisitedKeys)
        assertSame(beforeDelta[150], afterDelta[150])
        assertSame(beforeDelta[151], afterDelta[151])
        assertNotSame(beforeDelta[152], afterDelta[152])
        assertEquals("Streaming answer", afterDelta[152].response)
    }

    @Test
    fun `token replacement does not scan the settled live timeline prefix`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        lateinit var initialProjectedTurns: IteratorCountingList<ConversationTurnUiModel>
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                IteratorCountingList(projector.project(baseline, realtime)).also {
                    initialProjectedTurns = it
                }
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
        )
        val previousValues = buildList {
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
            add(SessionTimelineItem.User(key = "user-active", text = "Active question"))
            add(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-active",
                    text = "Streaming",
                    status = AssistantTurnStatus.STREAMING,
                ),
            )
        }
        val previousTimeline = TimelineReadCountingList(previousValues)
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-bounded-live-tail"),
            connectionEpoch = 4,
        ).copy(
            running = true,
            activeAssistantTurnKey = "assistant-active",
            timeline = previousTimeline,
        )
        val beforeDelta = cache.project(transcript, initial)

        val currentValues = previousValues.toMutableList().apply {
            this[lastIndex] = (last() as SessionTimelineItem.AssistantTurn).copy(
                text = "Streaming answer",
            )
        }
        val currentTimeline = TimelineReadCountingList(currentValues)
        val afterDelta = cache.project(
            transcript,
            initial.copy(
                timeline = currentTimeline,
                timelineMutation = RealtimeTimelineMutation(
                    sourceTimeline = previousTimeline,
                    firstChangedIndex = previousTimeline.lastIndex,
                ),
            ),
        )

        assertEquals(1, fullProjectionCalls)
        assertSame(beforeDelta.first(), afterDelta.first())
        assertNotSame(beforeDelta.last(), afterDelta.last())
        assertEquals("Streaming answer", afterDelta.last().response)
        assertEquals(0, initialProjectedTurns.iteratorCalls)
        assertEquals(
            true,
            currentTimeline.readCount <= 8,
            "Incremental index work read ${currentTimeline.readCount} live timeline items.",
        )
    }

    @Test
    fun `source bound token replacement does not scan settled live turn boundaries`() {
        val transcript = transcript()
        val projector = ConversationTurnProjector()
        val baseline = projector.projectBaseline(transcript)
        val previousTimeline = buildList {
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
            add(SessionTimelineItem.User(key = "user-active", text = "Active question"))
            add(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-active",
                    text = "Streaming",
                    status = AssistantTurnStatus.STREAMING,
                ),
            )
        }
        val previous = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-bounded-boundaries"),
            connectionEpoch = 5,
        ).copy(
            running = true,
            activeAssistantTurnKey = "assistant-active",
            timeline = previousTimeline,
        )
        val plainIndex = buildConversationTurnProjectionIndex(baseline, previous)
        val countedRanges = TimelineReadCountingList(plainIndex.liveTurns)
        val previousIndex = plainIndex.copy(liveTurns = countedRanges)
        val previousTurns = projector.project(baseline, previous)
        val currentTimeline = previousTimeline.toMutableList().apply {
            this[lastIndex] = (last() as SessionTimelineItem.AssistantTurn).copy(
                text = "Streaming answer",
            )
        }
        val current = previous.copy(
            timeline = currentTimeline,
            timelineMutation = RealtimeTimelineMutation(
                sourceTimeline = previousTimeline,
                firstChangedIndex = previousTimeline.lastIndex,
            ),
        )

        val projected = projectConversationTurnsIncrementally(
            baseline = baseline,
            previousIndex = previousIndex,
            previousRealtime = previous,
            previousTurns = previousTurns,
            realtime = current,
            onTimelineItemVisit = {},
        )

        check(projected != null)
        assertEquals("Streaming answer", projected.turns.last().response)
        assertEquals(
            true,
            countedRanges.readCount <= 2,
            "Source-bound replacement read ${countedRanges.readCount} settled live-turn boundaries.",
        )
    }

    @Test
    fun `historical pending tail absorption updates the correct output ordinal incrementally`() {
        val transcript = transcriptWithPendingTail(completedTurnCount = 2)
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
        )
        val initial = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-absorbed-tail"),
            connectionEpoch = 9,
        ).copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-absorbed",
                    text = "Absorbed answer",
                    status = AssistantTurnStatus.COMPLETE,
                ),
                SessionTimelineItem.User(key = "user-next", text = "Next question"),
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-next",
                    text = "Next answer",
                    status = AssistantTurnStatus.STREAMING,
                ),
            ),
        )
        val first = cache.project(transcript, initial)

        val absorbedUpdate = initial.copy(
            timeline = initial.timeline.map { item ->
                if (item is SessionTimelineItem.AssistantTurn && item.key == "assistant-absorbed") {
                    item.copy(text = "Absorbed answer updated")
                } else {
                    item
                }
            },
        )
        val second = cache.project(transcript, absorbedUpdate)

        assertEquals(1, fullProjectionCalls)
        assertEquals(4, second.size)
        assertSame(first[0], second[0])
        assertSame(first[1], second[1])
        assertNotSame(first[2], second[2])
        assertSame(first[3], second[3])
        assertEquals("Absorbed answer updated", second[2].response)
        assertEquals(projector.project(projector.projectBaseline(transcript), absorbedUpdate), second)

        val followingUpdate = absorbedUpdate.copy(
            timeline = absorbedUpdate.timeline.map { item ->
                if (item is SessionTimelineItem.AssistantTurn && item.key == "assistant-next") {
                    item.copy(text = "Next answer updated")
                } else {
                    item
                }
            },
        )
        val third = cache.project(transcript, followingUpdate)

        assertEquals(1, fullProjectionCalls)
        assertSame(second[2], third[2])
        assertNotSame(second[3], third[3])
        assertEquals("Next answer updated", third[3].response)
        assertEquals(projector.project(projector.projectBaseline(transcript), followingUpdate), third)
    }

    @Test
    fun `first absorbed assistant and its process update preserve the shifted historical slot`() {
        val transcript = transcriptWithPendingTail(completedTurnCount = 2)
        val projector = ConversationTurnProjector()
        var fullProjectionCalls = 0
        val cache = ConversationTurnProjectionCache(
            projectBaseline = projector::projectBaseline,
            projectRealtime = { baseline, realtime ->
                fullProjectionCalls += 1
                projector.project(baseline, realtime)
            },
            projectRealtimeIncrementally = ::projectConversationTurnsIncrementally,
        )
        val emptyLive = RealtimeSessionReducer().seed(
            transcript = transcript,
            runtimeSessionId = RuntimeSessionId("runtime-first-absorbed-tail"),
            connectionEpoch = 10,
        )
        val beforeAssistant = cache.project(transcript, emptyLive)
        val withAssistant = emptyLive.copy(
            running = true,
            timeline = listOf(
                SessionTimelineItem.AssistantTurn(
                    key = "assistant-absorbed",
                    text = "Absorbed answer",
                    status = AssistantTurnStatus.STREAMING,
                ),
                SessionTimelineItem.ProcessActivity(
                    key = "assistant-absorbed:process",
                    turnKey = "assistant-absorbed",
                ),
            ),
        )
        val afterAssistant = cache.project(transcript, withAssistant)

        assertEquals(1, fullProjectionCalls)
        assertEquals(3, afterAssistant.size)
        assertSame(beforeAssistant[0], afterAssistant[0])
        assertSame(beforeAssistant[1], afterAssistant[1])
        assertNotSame(beforeAssistant[2], afterAssistant[2])
        assertEquals("Absorbed answer", afterAssistant[2].response)
        assertEquals(projector.project(projector.projectBaseline(transcript), withAssistant), afterAssistant)

        val withProcess = withAssistant.copy(
            subagents = listOf(
                LiveSubagentProjection(
                    key = "assistant-absorbed:subagent:child",
                    turnKey = "assistant-absorbed",
                    goal = "Inspect absorbed turn",
                    status = LiveSubagentStatus.RUNNING,
                ),
            ),
            timelineMutation = RealtimeTimelineMutation(sourceTimeline = withAssistant.timeline),
        )
        val afterProcess = cache.project(transcript, withProcess)

        assertEquals(1, fullProjectionCalls)
        assertSame(afterAssistant[0], afterProcess[0])
        assertSame(afterAssistant[1], afterProcess[1])
        assertNotSame(afterAssistant[2], afterProcess[2])
        assertEquals(
            1,
            afterProcess[2].sections
                .filterIsInstance<HermesConversationSection.Subagents>()
                .single()
                .subagents
                .size,
        )
        assertEquals(projector.project(projector.projectBaseline(transcript), withProcess), afterProcess)
    }

    private fun transcript() = SessionTranscript(
        sessionKey = SessionKey("stored-1"),
        lineageTip = SessionKey("stored-1"),
        messages = emptyList(),
        pagination = TranscriptPagination(
            limit = 0,
            offset = 0,
            returned = 0,
        ),
    )

    private fun transcriptWithCompletedTurns(count: Int): SessionTranscript {
        val messages = buildList {
            repeat(count) { index ->
                val userId = index.toLong() * 2 + 1
                val assistantId = userId + 1
                add(
                    SessionMessageProjection(
                        messageId = userId,
                        role = "user",
                        content = JsonPrimitive("Historical question $index"),
                        timestampEpochSeconds = userId.toDouble(),
                        reasoning = null,
                        reasoningContent = null,
                        reasoningDetails = null,
                        toolCallId = null,
                        toolCalls = null,
                        toolName = null,
                        displayKind = null,
                        displayMetadata = null,
                    ),
                )
                add(
                    SessionMessageProjection(
                        messageId = assistantId,
                        role = "assistant",
                        content = JsonPrimitive("Historical answer $index"),
                        timestampEpochSeconds = assistantId.toDouble(),
                        reasoning = null,
                        reasoningContent = null,
                        reasoningDetails = null,
                        toolCallId = null,
                        toolCalls = null,
                        toolName = null,
                        displayKind = null,
                        displayMetadata = null,
                    ),
                )
            }
        }
        return SessionTranscript(
            sessionKey = SessionKey("stored-large"),
            lineageTip = SessionKey("stored-large"),
            messages = messages,
            pagination = TranscriptPagination(
                limit = messages.size,
                offset = 0,
                returned = messages.size,
            ),
        )
    }

    private fun transcriptWithPendingTail(completedTurnCount: Int): SessionTranscript {
        val completed = transcriptWithCompletedTurns(completedTurnCount)
        val pendingMessageId = completedTurnCount.toLong() * 2 + 1
        val pending = SessionMessageProjection(
            messageId = pendingMessageId,
            role = "user",
            content = JsonPrimitive("Pending historical question"),
            timestampEpochSeconds = pendingMessageId.toDouble(),
            reasoning = null,
            reasoningContent = null,
            reasoningDetails = null,
            toolCallId = null,
            toolCalls = null,
            toolName = null,
            displayKind = null,
            displayMetadata = null,
        )
        val messages = completed.messages + pending
        return completed.copy(
            messages = messages,
            pagination = completed.pagination.copy(
                limit = messages.size,
                returned = messages.size,
            ),
        )
    }

    private fun projectedTurn(
        key: String,
        response: String,
    ) = ConversationTurnUiModel(
        key = key,
        userPrompt = null,
        thinking = "",
        statusText = "",
        tools = emptyList(),
        response = response,
        status = ConversationTurnStatus.STREAMING,
    )

    private class IteratorCountingList<T>(
        private val values: List<T>,
    ) : AbstractList<T>() {
        var iteratorCalls: Int = 0
            private set

        override val size: Int
            get() = values.size

        override fun get(index: Int): T = values[index]

        override fun iterator(): Iterator<T> {
            iteratorCalls += 1
            return values.iterator()
        }
    }

    private class TimelineReadCountingList<T>(
        private val values: List<T>,
    ) : AbstractList<T>() {
        var readCount: Int = 0
            private set

        override val size: Int
            get() = values.size

        override fun get(index: Int): T {
            readCount += 1
            return values[index]
        }
    }
}
