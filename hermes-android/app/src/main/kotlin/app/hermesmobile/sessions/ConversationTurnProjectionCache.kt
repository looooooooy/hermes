package app.hermesmobile.sessions

import app.hermesmobile.protocol.sessions.SessionTranscript

internal class ConversationTurnProjectionCache(
    private val projectBaseline: (SessionTranscript) -> ConversationTurnProjectionBaseline,
    private val projectRealtime: (
        ConversationTurnProjectionBaseline,
        RealtimeSessionProjection?,
    ) -> List<ConversationTurnUiModel>,
    private val projectRealtimeIncrementally: ((
        ConversationTurnProjectionBaseline,
        ConversationTurnProjectionIndex,
        RealtimeSessionProjection,
        List<ConversationTurnUiModel>,
        RealtimeSessionProjection,
        (SessionTimelineItem) -> Unit,
    ) -> IncrementalConversationTurnProjection?)? = null,
    private val onIncrementalTimelineItemVisit: (SessionTimelineItem) -> Unit = {},
) {
    private var sourceTranscript: SessionTranscript? = null
    private var sourceRealtime: RealtimeSessionProjection? = null
    private var baseline: ConversationTurnProjectionBaseline? = null
    private var projectionIndex: ConversationTurnProjectionIndex? = null
    private var projectedTurns: List<ConversationTurnUiModel> = emptyList()
    private var hasProjection: Boolean = false

    fun project(
        transcript: SessionTranscript,
        realtime: RealtimeSessionProjection?,
    ): List<ConversationTurnUiModel> {
        if (sourceTranscript !== transcript) {
            sourceTranscript = transcript
            sourceRealtime = null
            baseline = projectBaseline(transcript)
            projectionIndex = null
            projectedTurns = emptyList()
            hasProjection = false
        } else if (hasProjection && sourceRealtime === realtime) {
            return projectedTurns
        }
        val currentBaseline = requireNotNull(baseline)
        val previousRealtime = sourceRealtime
        val previousIndex = projectionIndex
        if (
            hasProjection &&
            previousRealtime != null &&
            previousIndex != null &&
            realtime != null &&
            projectRealtimeIncrementally != null
        ) {
            projectRealtimeIncrementally(
                currentBaseline,
                previousIndex,
                previousRealtime,
                projectedTurns,
                realtime,
                onIncrementalTimelineItemVisit,
            )?.let { incremental ->
                projectedTurns = incremental.turns
                projectionIndex = incremental.index
                sourceRealtime = realtime
                hasProjection = true
                return projectedTurns
            }
        }

        val canRetainPreviousInstances = previousRealtime == null || realtime == null || (
            previousRealtime.runtimeSessionId == realtime.runtimeSessionId &&
                previousRealtime.connectionEpoch == realtime.connectionEpoch
            )
        val freshTurns = projectRealtime(currentBaseline, realtime)
        if (projectedTurns.isEmpty() || !canRetainPreviousInstances) {
            projectedTurns = freshTurns
        } else {
            var previousByKey: Map<String, ConversationTurnUiModel>? = null
            val stableTurns = freshTurns.mapIndexed { index, fresh ->
                val aligned = projectedTurns.getOrNull(index)
                    ?.takeIf { previous -> previous.key == fresh.key }
                val previous = aligned ?: run {
                    val keyed = previousByKey ?: projectedTurns
                        .associateBy(ConversationTurnUiModel::key)
                        .also { previousByKey = it }
                    keyed[fresh.key]
                }
                previous?.takeIf { it === fresh || it == fresh } ?: fresh
            }
            val allInstancesRetained = stableTurns.size == projectedTurns.size &&
                stableTurns.indices.all { index -> stableTurns[index] === projectedTurns[index] }
            if (!allInstancesRetained) {
                projectedTurns = stableTurns
            }
        }
        projectionIndex = buildConversationTurnProjectionIndex(currentBaseline, realtime)
        sourceRealtime = realtime
        hasProjection = true
        return projectedTurns
    }
}
