package app.hermesmobile.sessions

import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TranscriptMinimapPresentationTest {
    @Test
    fun `markers preserve authoritative turn and task hierarchy order`() {
        val todo = HermesConversationSection.Todo(
            metadata = metadata("todo", HermesConversationSectionStatus.RUNNING),
            items = listOf(
                HermesConversationTodoItem(
                    key = "todo-1",
                    content = "Read the contract",
                    status = HermesConversationTodoStatus.COMPLETED,
                ),
                HermesConversationTodoItem(
                    key = "todo-2",
                    content = "Wire Cloud observer",
                    status = HermesConversationTodoStatus.IN_PROGRESS,
                ),
            ),
        )
        val subagents = HermesConversationSection.Subagents(
            metadata = metadata("subagents", HermesConversationSectionStatus.RUNNING),
            subagents = listOf(
                HermesConversationSubagent(
                    key = "agent-parent",
                    goal = "Implement Android parity",
                    status = HermesConversationSectionStatus.RUNNING,
                ),
                HermesConversationSubagent(
                    key = "agent-child",
                    goal = "Verify long transcript",
                    status = HermesConversationSectionStatus.PENDING,
                    parentKey = "agent-parent",
                ),
            ),
            moaReferences = emptyList(),
        )
        val turns = listOf(
            turn("turn-1", "Connect Android to Cloud"),
            turn(
                key = "turn-2",
                prompt = "Finish the mobile loop",
                sections = CanonicalConversationSections.of(todo, subagents),
            ),
        )

        val markers = buildTranscriptMinimapMarkers(turns)

        assertEquals(
            listOf(
                "turn-1",
                "turn-2",
                "turn-2:todo:todo-1",
                "turn-2:todo:todo-2",
                "turn-2:subagent:agent-parent",
                "turn-2:subagent:agent-child",
            ),
            markers.map(TranscriptMinimapMarker::key),
        )
        assertEquals(
            listOf(0, 0, 1, 1, 1, 2),
            markers.map(TranscriptMinimapMarker::depth),
        )
        assertEquals(
            listOf(1, 2, 2, 2, 2, 2),
            markers.map(TranscriptMinimapMarker::turnOrdinal),
        )
        assertEquals(TranscriptMinimapStatus.RUNNING, markers[3].status)
        assertEquals("Wire Cloud observer", markers[3].summary)
    }

    @Test
    fun `short content hides minimap while a scrollable long conversation shows it`() {
        assertFalse(shouldShowTranscriptMinimap(markerCount = 7, viewportScrollable = true))
        assertFalse(shouldShowTranscriptMinimap(markerCount = 8, viewportScrollable = false))
        assertTrue(shouldShowTranscriptMinimap(markerCount = 8, viewportScrollable = true))
    }

    @Test
    fun `visible list item selects its turn marker even with a history header`() {
        val markers = buildTranscriptMinimapMarkers(
            listOf(
                turn("turn-1", "First"),
                turn("turn-2", "Second"),
                turn("turn-3", "Third"),
            ),
        )
        val renderedItemKeys = listOf(
            TRANSCRIPT_HISTORY_HEADER_KEY,
            "turn-1",
            "turn-2",
            "turn-3",
        )

        assertEquals(
            1,
            activeTranscriptMinimapMarkerIndex(
                markers = markers,
                renderedItemKeys = renderedItemKeys,
                firstVisibleItemIndex = 2,
            ),
        )
        assertEquals(
            0,
            activeTranscriptMinimapMarkerIndex(
                markers = markers,
                renderedItemKeys = renderedItemKeys,
                firstVisibleItemIndex = 0,
            ),
        )
        assertNull(
            activeTranscriptMinimapMarkerIndex(
                markers = emptyList(),
                renderedItemKeys = renderedItemKeys,
                firstVisibleItemIndex = 0,
            ),
        )
    }

    @Test
    fun `tap and scrub positions clamp to the closest marker`() {
        assertNull(transcriptMinimapTargetMarkerIndex(10f, 100f, 0))
        assertEquals(0, transcriptMinimapTargetMarkerIndex(-20f, 100f, 5))
        assertEquals(2, transcriptMinimapTargetMarkerIndex(50f, 100f, 5))
        assertEquals(4, transcriptMinimapTargetMarkerIndex(120f, 100f, 5))
        assertEquals(0, transcriptMinimapTargetMarkerIndex(20f, 0f, 5))
    }

    @Test
    fun `dense visual layout caps markers by track geometry and keeps priority markers`() {
        val markers = (0 until 100).map { index ->
            marker(
                index = index,
                kind = if (index % 5 == 0) {
                    TranscriptMinimapMarkerKind.TODO
                } else {
                    TranscriptMinimapMarkerKind.TURN
                },
                status = when (index) {
                    30 -> TranscriptMinimapStatus.RUNNING
                    70 -> TranscriptMinimapStatus.ERROR
                    else -> TranscriptMinimapStatus.COMPLETE
                },
            )
        }

        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 50,
            trackStartY = 24f,
            trackHeight = 42f,
            markerStrokeWidth = 3f,
            minimumGap = 3f,
        )

        assertEquals(8, layout.size)
        assertTrue(
            layout.map { it.markerIndex }.containsAll(listOf(0, 30, 50, 70, 99)),
            layout.map { it.markerIndex }.toString(),
        )
        assertEquals(
            layout.map { it.markerIndex }.sorted(),
            layout.map { it.markerIndex },
        )
        layout.zipWithNext { first, second ->
            assertTrue(second.centerY - first.centerY >= 6f)
        }
    }

    @Test
    fun `active marker remains visible when the track only fits one line`() {
        val markers = (0 until 20).map(::marker)

        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 13,
            trackStartY = 10f,
            trackHeight = 1f,
            markerStrokeWidth = 3f,
            minimumGap = 3f,
        )

        assertEquals(listOf(13), layout.map { it.markerIndex })
        assertEquals(10.5f, layout.single().centerY)
    }

    @Test
    fun `very tall dense minimap stays bounded and aggregates every marker`() {
        val markers = (0 until 1_000).map(::marker)

        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 503,
            trackStartY = 24f,
            trackHeight = 10_000f,
            markerStrokeWidth = 1f,
            minimumGap = 0f,
        )

        assertTrue(layout.size <= 32)
        assertTrue(layout.any { it.markerIndex == 503 })
        assertEquals(0, layout.first().bucketStartIndex)
        assertEquals(markers.lastIndex, layout.last().bucketEndIndex)
        layout.zipWithNext { first, second ->
            assertEquals(first.bucketEndIndex + 1, second.bucketStartIndex)
        }
    }

    @Test
    fun `critical states represent aggregate slots ahead of ordinary endpoints`() {
        val markers = (0 until 1_000).map { index ->
            marker(
                index = index,
                status = when (index) {
                    5 -> TranscriptMinimapStatus.ERROR
                    995 -> TranscriptMinimapStatus.RUNNING
                    else -> TranscriptMinimapStatus.COMPLETE
                },
            )
        }

        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 503,
            trackStartY = 24f,
            trackHeight = 10_000f,
            markerStrokeWidth = 1f,
            minimumGap = 0f,
        )

        assertEquals(5, layout.first().markerIndex)
        assertEquals(TranscriptMinimapStatus.ERROR, markers[layout.first().markerIndex].status)
        assertEquals(995, layout.last().markerIndex)
        assertEquals(TranscriptMinimapStatus.RUNNING, markers[layout.last().markerIndex].status)
    }

    @Test
    fun `visual slots stay aligned with authoritative progress when priorities cluster`() {
        val markers = (0..100).map { index ->
            marker(
                index = index,
                status = when (index) {
                    1, 99 -> TranscriptMinimapStatus.ERROR
                    2, 98 -> TranscriptMinimapStatus.RUNNING
                    else -> TranscriptMinimapStatus.COMPLETE
                },
            )
        }

        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 3,
            trackStartY = 12f,
            trackHeight = 60f,
            markerStrokeWidth = 3f,
            minimumGap = 3f,
        )

        assertEquals(11, layout.size)
        layout.forEach { visualMarker ->
            val mappedIndex = transcriptMinimapTargetMarkerIndex(
                pointerY = visualMarker.centerY - 12f,
                trackHeight = 60f,
                markerCount = markers.size,
            )
            val targetIndex = checkNotNull(mappedIndex)
            assertTrue(targetIndex in visualMarker.bucketStartIndex..visualMarker.bucketEndIndex)
            assertTrue(abs(targetIndex - visualMarker.markerIndex) <= 5)
        }
        val activeVisual = layout.single { it.markerIndex == 3 }
        val activeProgress = (activeVisual.centerY - 12f) / 60f
        assertTrue(abs(activeProgress - 3f / 100f) <= 0.05f)
        layout.zipWithNext { first, second ->
            assertTrue(second.centerY - first.centerY >= 6f)
        }
    }

    @Test
    fun `visible line centers select their exact priority marker while empty track stays continuous`() {
        val markers = (0..100).map { index ->
            marker(
                index = index,
                kind = if (index == 27) {
                    TranscriptMinimapMarkerKind.TODO
                } else {
                    TranscriptMinimapMarkerKind.TURN
                },
                status = when (index) {
                    8 -> TranscriptMinimapStatus.ERROR
                    18 -> TranscriptMinimapStatus.RUNNING
                    27 -> TranscriptMinimapStatus.PENDING
                    else -> TranscriptMinimapStatus.COMPLETE
                },
            )
        }
        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 43,
            trackStartY = 12f,
            trackHeight = 60f,
            markerStrokeWidth = 3f,
            minimumGap = 3f,
        )

        layout.forEach { visualMarker ->
            assertEquals(
                visualMarker.markerIndex,
                transcriptMinimapPointerTargetMarkerIndex(
                    pointerY = visualMarker.centerY,
                    trackStartY = 12f,
                    trackHeight = 60f,
                    markerCount = markers.size,
                    visualLayout = layout,
                    visibleMarkerHitRadius = 0.25f,
                ),
            )
        }

        val emptyTrackY = 33.3f
        assertEquals(
            transcriptMinimapTargetMarkerIndex(
                pointerY = emptyTrackY - 12f,
                trackHeight = 60f,
                markerCount = markers.size,
            ),
            transcriptMinimapPointerTargetMarkerIndex(
                pointerY = emptyTrackY,
                trackStartY = 12f,
                trackHeight = 60f,
                markerCount = markers.size,
                visualLayout = layout,
                visibleMarkerHitRadius = 0.25f,
            ),
        )
    }

    @Test
    fun `production hit radius keeps every hidden dense marker reachable`() {
        val trackStartY = 12f
        val trackHeight = 60f
        val markers = (0..100).map { index ->
            marker(
                index = index,
                kind = if (index == 27) {
                    TranscriptMinimapMarkerKind.TODO
                } else {
                    TranscriptMinimapMarkerKind.TURN
                },
                status = when (index) {
                    8 -> TranscriptMinimapStatus.ERROR
                    18 -> TranscriptMinimapStatus.RUNNING
                    27 -> TranscriptMinimapStatus.PENDING
                    else -> TranscriptMinimapStatus.COMPLETE
                },
            )
        }
        val layout = transcriptMinimapVisualLayout(
            markers = markers,
            activeMarkerIndex = 43,
            trackStartY = trackStartY,
            trackHeight = trackHeight,
            markerStrokeWidth = 3f,
            minimumGap = 2f,
        )
        val productionHitRadius = transcriptMinimapVisibleMarkerHitRadius(
            trackHeight = trackHeight,
            markerCount = markers.size,
            preferredHitRadius = (3f + 2f) / 2f,
        )
        val authoritativeStep = trackHeight / markers.lastIndex

        layout.forEach { visualMarker ->
            assertEquals(
                visualMarker.markerIndex,
                transcriptMinimapPointerTargetMarkerIndex(
                    pointerY = visualMarker.centerY,
                    trackStartY = trackStartY,
                    trackHeight = trackHeight,
                    markerCount = markers.size,
                    visualLayout = layout,
                    visibleMarkerHitRadius = productionHitRadius,
                ),
            )
        }
        val visibleIndices = layout.mapTo(mutableSetOf()) { it.markerIndex }
        markers.indices
            .filterNot(visibleIndices::contains)
            .forEach { hiddenIndex ->
                val centerY = trackStartY + authoritativeStep * hiddenIndex
                val reachable = (-4..4).any { sample ->
                    val pointerY = centerY + authoritativeStep * sample / 10f
                    transcriptMinimapPointerTargetMarkerIndex(
                        pointerY = pointerY,
                        trackStartY = trackStartY,
                        trackHeight = trackHeight,
                        markerCount = markers.size,
                        visualLayout = layout,
                        visibleMarkerHitRadius = productionHitRadius,
                    ) == hiddenIndex
                }
                assertTrue(reachable, "Hidden marker $hiddenIndex must remain reachable.")
            }
    }

    @Test
    fun `marker summary is compact stable and never uses tool output`() {
        val noisy = "  Summarize   the\nCloud observer contract " + "x".repeat(100)
        val marker = buildTranscriptMinimapMarkers(
            listOf(
                turn(
                    key = "turn-1",
                    prompt = noisy,
                    sections = CanonicalConversationSections.of(
                        HermesConversationSection.ToolGroup(
                            metadata = metadata(
                                "tools",
                                HermesConversationSectionStatus.COMPLETE,
                            ),
                            tools = listOf(
                                ConversationToolUiModel(
                                    key = "tool",
                                    toolId = "tool",
                                    name = null,
                                    output = "secret tool output",
                                    status = ConversationToolStatus.COMPLETE,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ).single()

        assertTrue(marker.summary.startsWith("Summarize the Cloud observer contract"))
        assertTrue(marker.summary.length <= TRANSCRIPT_MINIMAP_SUMMARY_LIMIT)
        assertFalse(marker.summary.contains("secret"))
    }

    private fun turn(
        key: String,
        prompt: String,
        sections: CanonicalConversationSections = CanonicalConversationSections.Empty,
    ) = ConversationTurnUiModel(
        key = key,
        userPrompt = ConversationPromptUiModel("$key:prompt", prompt),
        thinking = "",
        statusText = "",
        tools = emptyList(),
        response = "",
        status = ConversationTurnStatus.COMPLETE,
        sections = sections,
    )

    private fun metadata(
        key: String,
        status: HermesConversationSectionStatus,
    ) = HermesConversationSectionMetadata(key = key, status = status)

    private fun marker(
        index: Int,
        kind: TranscriptMinimapMarkerKind = TranscriptMinimapMarkerKind.TURN,
        status: TranscriptMinimapStatus = TranscriptMinimapStatus.COMPLETE,
    ) = TranscriptMinimapMarker(
        key = "marker-$index",
        turnKey = "turn-$index",
        turnOrdinal = index + 1,
        kind = kind,
        depth = if (kind == TranscriptMinimapMarkerKind.TURN) 0 else 1,
        status = status,
        summary = "Marker $index",
    )
}
