package app.hermesmobile.sessions

import kotlin.math.floor
import kotlin.math.abs
import kotlin.math.min
import kotlin.math.roundToInt

internal const val TRANSCRIPT_MINIMAP_SUMMARY_LIMIT = 64
internal const val TRANSCRIPT_MINIMAP_MIN_MARKERS = 8
internal const val TRANSCRIPT_MINIMAP_MAX_RENDERED_MARKERS = 32

internal enum class TranscriptMinimapMarkerKind {
    TURN,
    TODO,
    SUBAGENT,
}

internal enum class TranscriptMinimapStatus {
    PENDING,
    RUNNING,
    COMPLETE,
    INTERRUPTED,
    ERROR,
}

internal data class TranscriptMinimapMarker(
    val key: String,
    val turnKey: String,
    val turnOrdinal: Int,
    val kind: TranscriptMinimapMarkerKind,
    val depth: Int,
    val status: TranscriptMinimapStatus,
    val summary: String,
    val disclosureSection: ConversationDisclosureSection? = null,
)

internal data class TranscriptMinimapVisualMarker(
    val markerIndex: Int,
    val centerY: Float,
    val bucketStartIndex: Int,
    val bucketEndIndex: Int,
)

internal fun buildTranscriptMinimapMarkers(
    turns: List<ConversationTurnUiModel>,
): List<TranscriptMinimapMarker> = buildList {
    turns.forEachIndexed { turnIndex, turn ->
        val turnOrdinal = turnIndex + 1
        add(
            TranscriptMinimapMarker(
                key = turn.key,
                turnKey = turn.key,
                turnOrdinal = turnOrdinal,
                kind = TranscriptMinimapMarkerKind.TURN,
                depth = 0,
                status = turn.status.toMinimapStatus(),
                summary = turn.minimapSummary(),
            ),
        )
        turn.sections.forEach { section ->
            when (section) {
                is HermesConversationSection.Todo -> section.items.forEach { item ->
                    add(
                        TranscriptMinimapMarker(
                            key = "${turn.key}:todo:${item.key}",
                            turnKey = turn.key,
                            turnOrdinal = turnOrdinal,
                            kind = TranscriptMinimapMarkerKind.TODO,
                            depth = 1,
                            status = item.status.toMinimapStatus(),
                            summary = item.content.toMinimapSummary(),
                            disclosureSection = ConversationDisclosureSection.TODO,
                        ),
                    )
                }

                is HermesConversationSection.Subagents -> {
                    val depths = mutableMapOf<String, Int>()
                    section.subagents.forEach { subagent ->
                        val depth = subagent.parentKey
                            ?.let(depths::get)
                            ?.plus(1)
                            ?: 1
                        depths[subagent.key] = depth
                        add(
                            TranscriptMinimapMarker(
                                key = "${turn.key}:subagent:${subagent.key}",
                                turnKey = turn.key,
                                turnOrdinal = turnOrdinal,
                                kind = TranscriptMinimapMarkerKind.SUBAGENT,
                                depth = depth,
                                status = subagent.status.toMinimapStatus(),
                                summary = subagent.goal.toMinimapSummary(),
                                disclosureSection = ConversationDisclosureSection.SUBAGENTS,
                            ),
                        )
                    }
                }

                else -> Unit
            }
        }
    }
}

internal fun shouldShowTranscriptMinimap(
    markerCount: Int,
    viewportScrollable: Boolean,
): Boolean = viewportScrollable && markerCount >= TRANSCRIPT_MINIMAP_MIN_MARKERS

internal fun activeTranscriptMinimapMarkerIndex(
    markers: List<TranscriptMinimapMarker>,
    renderedItemKeys: List<String>,
    firstVisibleItemIndex: Int,
): Int? {
    if (markers.isEmpty() || renderedItemKeys.isEmpty()) return null
    val boundedIndex = firstVisibleItemIndex.coerceIn(renderedItemKeys.indices)
    val visibleTurnKey = renderedItemKeys
        .subList(boundedIndex, renderedItemKeys.size)
        .firstOrNull { key -> markers.any { it.turnKey == key } }
        ?: renderedItemKeys
            .subList(0, boundedIndex + 1)
            .asReversed()
            .firstOrNull { key -> markers.any { it.turnKey == key } }
        ?: return null
    return markers.indexOfFirst { marker ->
        marker.turnKey == visibleTurnKey && marker.kind == TranscriptMinimapMarkerKind.TURN
    }.takeIf { it >= 0 }
}

internal fun transcriptMinimapTargetMarkerIndex(
    pointerY: Float,
    trackHeight: Float,
    markerCount: Int,
): Int? {
    if (markerCount <= 0) return null
    if (trackHeight <= 0f || markerCount == 1) return 0
    val progress = (pointerY / trackHeight).coerceIn(0f, 1f)
    return (progress * (markerCount - 1)).roundToInt()
}

internal fun transcriptMinimapPointerTargetMarkerIndex(
    pointerY: Float,
    trackStartY: Float,
    trackHeight: Float,
    markerCount: Int,
    visualLayout: List<TranscriptMinimapVisualMarker>,
    visibleMarkerHitRadius: Float,
): Int? {
    if (markerCount <= 0) return null
    val hitRadius = visibleMarkerHitRadius.coerceAtLeast(0f)
    val visibleHit = visualLayout
        .asSequence()
        .map { marker -> marker to abs(pointerY - marker.centerY) }
        .filter { (_, distance) -> distance <= hitRadius }
        .minWithOrNull(
            compareBy<Pair<TranscriptMinimapVisualMarker, Float>> { (_, distance) -> distance }
                .thenBy { (marker, _) -> marker.markerIndex },
        )
        ?.first
    if (visibleHit != null) return visibleHit.markerIndex
    return transcriptMinimapTargetMarkerIndex(
        pointerY = pointerY - trackStartY,
        trackHeight = trackHeight,
        markerCount = markerCount,
    )
}

internal fun transcriptMinimapVisibleMarkerHitRadius(
    trackHeight: Float,
    markerCount: Int,
    preferredHitRadius: Float,
): Float {
    val boundedPreferredRadius = preferredHitRadius.coerceAtLeast(0f)
    if (markerCount <= 1) return boundedPreferredRadius
    val authoritativeMarkerSpacing =
        trackHeight.coerceAtLeast(0f) / (markerCount - 1)
    return min(
        boundedPreferredRadius,
        authoritativeMarkerSpacing * TRANSCRIPT_MINIMAP_SNAP_SPACING_FRACTION,
    )
}

internal fun transcriptMinimapVisualLayout(
    markers: List<TranscriptMinimapMarker>,
    activeMarkerIndex: Int?,
    trackStartY: Float,
    trackHeight: Float,
    markerStrokeWidth: Float,
    minimumGap: Float,
): List<TranscriptMinimapVisualMarker> {
    if (markers.isEmpty()) return emptyList()
    val boundedTrackHeight = trackHeight.coerceAtLeast(0f)
    val minimumSpacing = (markerStrokeWidth.coerceAtLeast(0f) +
        minimumGap.coerceAtLeast(0f)).coerceAtLeast(1f)
    val capacity = (floor(boundedTrackHeight / minimumSpacing).toInt() + 1)
        .coerceIn(
            minimumValue = 1,
            maximumValue = min(markers.size, TRANSCRIPT_MINIMAP_MAX_RENDERED_MARKERS),
        )
    val buckets = List(capacity) { mutableListOf<Int>() }
    markers.indices.forEach { markerIndex ->
        buckets[
            transcriptMinimapVisualSlotIndex(
                markerIndex = markerIndex,
                markerCount = markers.size,
                slotCount = capacity,
            )
        ] += markerIndex
    }
    return buckets.mapIndexed { visualSlot, candidates ->
        val slotProgress = if (capacity == 1) {
            0.5f
        } else {
            visualSlot.toFloat() / (capacity - 1)
        }
        val markerIndex = candidates.minWith(
            compareBy<Int> { candidateIndex ->
                transcriptMinimapVisualPriority(
                    marker = markers[candidateIndex],
                    active = candidateIndex == activeMarkerIndex,
                    endpoint = candidateIndex == 0 || candidateIndex == markers.lastIndex,
                )
            }.thenBy { candidateIndex ->
                val markerProgress = if (markers.size == 1) {
                    0.5f
                } else {
                    candidateIndex.toFloat() / markers.lastIndex
                }
                abs(markerProgress - slotProgress)
            }.thenBy { it },
        )
        val centerY = if (capacity == 1) {
            trackStartY + boundedTrackHeight / 2f
        } else {
            trackStartY +
                boundedTrackHeight * visualSlot / (capacity - 1)
        }
        TranscriptMinimapVisualMarker(
            markerIndex = markerIndex,
            centerY = centerY,
            bucketStartIndex = candidates.first(),
            bucketEndIndex = candidates.last(),
        )
    }
}

private fun transcriptMinimapVisualSlotIndex(
    markerIndex: Int,
    markerCount: Int,
    slotCount: Int,
): Int {
    if (markerCount <= 1 || slotCount <= 1) return 0
    return (
        markerIndex.toFloat() * (slotCount - 1) / (markerCount - 1)
        ).roundToInt()
        .coerceIn(0, slotCount - 1)
}

private fun transcriptMinimapVisualPriority(
    marker: TranscriptMinimapMarker,
    active: Boolean,
    endpoint: Boolean,
): Int = when {
    active -> 0
    marker.status == TranscriptMinimapStatus.ERROR -> 1
    marker.status == TranscriptMinimapStatus.RUNNING -> 2
    endpoint -> 3
    marker.kind != TranscriptMinimapMarkerKind.TURN &&
        marker.status != TranscriptMinimapStatus.COMPLETE -> 4
    marker.kind == TranscriptMinimapMarkerKind.TURN -> 5
    else -> 6
}

private const val TRANSCRIPT_MINIMAP_SNAP_SPACING_FRACTION = 0.2f

private fun ConversationTurnUiModel.minimapSummary(): String = (
    userPrompt?.text
        ?: response.takeIf(String::isNotBlank)
        ?: error?.takeIf(String::isNotBlank)
        ?: eventText?.takeIf(String::isNotBlank)
        ?: statusText.takeIf(String::isNotBlank)
        ?: "Hermes"
    ).toMinimapSummary()

private fun String.toMinimapSummary(): String = trim()
    .replace(Regex("\\s+"), " ")
    .take(TRANSCRIPT_MINIMAP_SUMMARY_LIMIT)

private fun ConversationTurnStatus.toMinimapStatus(): TranscriptMinimapStatus = when (this) {
    ConversationTurnStatus.INCOMPLETE -> TranscriptMinimapStatus.PENDING
    ConversationTurnStatus.STREAMING -> TranscriptMinimapStatus.RUNNING
    ConversationTurnStatus.COMPLETE -> TranscriptMinimapStatus.COMPLETE
    ConversationTurnStatus.ERROR -> TranscriptMinimapStatus.ERROR
}

private fun HermesConversationTodoStatus.toMinimapStatus(): TranscriptMinimapStatus = when (this) {
    HermesConversationTodoStatus.PENDING -> TranscriptMinimapStatus.PENDING
    HermesConversationTodoStatus.IN_PROGRESS -> TranscriptMinimapStatus.RUNNING
    HermesConversationTodoStatus.COMPLETED -> TranscriptMinimapStatus.COMPLETE
    HermesConversationTodoStatus.CANCELLED -> TranscriptMinimapStatus.INTERRUPTED
}

private fun HermesConversationSectionStatus.toMinimapStatus(): TranscriptMinimapStatus = when (this) {
    HermesConversationSectionStatus.PENDING -> TranscriptMinimapStatus.PENDING
    HermesConversationSectionStatus.RUNNING,
    HermesConversationSectionStatus.STREAMING,
    -> TranscriptMinimapStatus.RUNNING

    HermesConversationSectionStatus.COMPLETE -> TranscriptMinimapStatus.COMPLETE
    HermesConversationSectionStatus.INTERRUPTED -> TranscriptMinimapStatus.INTERRUPTED
    HermesConversationSectionStatus.ERROR -> TranscriptMinimapStatus.ERROR
}
