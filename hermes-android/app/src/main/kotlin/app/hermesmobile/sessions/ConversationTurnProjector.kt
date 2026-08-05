package app.hermesmobile.sessions

import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull

/**
 * Immutable one-time projection of an authoritative historical transcript.
 *
 * Reuse this value across realtime deltas. Build a new baseline when REST
 * resync installs a new [SessionTranscript].
 */
class ConversationTurnProjectionBaseline internal constructor(
    val historicalTurns: List<ConversationTurnUiModel>,
    internal val historicalTail: HistoricalConversationTurnSnapshot?,
)

class ConversationTurnProjector {
    fun projectBaseline(transcript: SessionTranscript): ConversationTurnProjectionBaseline {
        val turns = transcript.toHistoricalTurns()
        return ConversationTurnProjectionBaseline(
            historicalTurns = turns.map(MutableConversationTurn::toUiModel),
            historicalTail = turns.lastOrNull()?.toSnapshot(),
        )
    }

    fun project(
        baseline: ConversationTurnProjectionBaseline,
        realtime: RealtimeSessionProjection?,
    ): List<ConversationTurnUiModel> {
        val timeline = realtime?.timeline ?: return baseline.historicalTurns
        if (timeline.isEmpty()) return baseline.historicalTurns

        val liveTurns = timeline.toLiveTurns()
        liveTurns.forEach { it.attachRealtimeProcess(realtime) }
        val historicalTail = baseline.historicalTail
        val liveHead = liveTurns.firstOrNull()
        return if (
            historicalTail?.canAcceptAssistant() == true &&
            liveHead != null &&
            liveHead.userPrompt == null
        ) {
            val activeTail = historicalTail.toMutable().apply { absorb(liveHead) }
            buildList(baseline.historicalTurns.size + liveTurns.size - 1) {
                addAll(baseline.historicalTurns.dropLast(1))
                add(activeTail.toUiModel())
                liveTurns.drop(1).mapTo(this, MutableConversationTurn::toUiModel)
            }
        } else {
            buildList(baseline.historicalTurns.size + liveTurns.size) {
                addAll(baseline.historicalTurns)
                liveTurns.mapTo(this, MutableConversationTurn::toUiModel)
            }
        }
    }

    fun project(
        transcript: SessionTranscript,
        realtime: RealtimeSessionProjection?,
    ): List<ConversationTurnUiModel> = project(projectBaseline(transcript), realtime)
}

internal data class ConversationLiveTurnRange(
    val startIndex: Int,
    val endExclusive: Int,
    val key: String,
    val sourceAssistantTurnKey: String?,
)

/** Metadata retained by the cache so live deltas can rebuild only affected logical turns. */
internal data class ConversationTurnProjectionIndex(
    val runtimeSessionId: app.hermesmobile.protocol.sessions.RuntimeSessionId?,
    val connectionEpoch: Long?,
    val timelineKeys: List<String>,
    val timelineItems: List<SessionTimelineItem>,
    val liveTurns: List<ConversationLiveTurnRange>,
    val liveTurnOrdinalByTimelineIndex: IntArray,
    val historicalTailAbsorbed: Boolean,
    val hasUnambiguousBoundaries: Boolean,
)

internal data class IncrementalConversationTurnProjection(
    val turns: List<ConversationTurnUiModel>,
    val index: ConversationTurnProjectionIndex,
)

private class ConversationTurnOverlayList private constructor(
    private val root: List<ConversationTurnUiModel>,
    private val replacements: Map<Int, ConversationTurnUiModel>,
) : AbstractList<ConversationTurnUiModel>() {
    override val size: Int
        get() = root.size

    override fun get(index: Int): ConversationTurnUiModel = replacements[index] ?: root[index]

    fun replacing(
        updates: Map<Int, ConversationTurnUiModel>,
    ): ConversationTurnOverlayList = ConversationTurnOverlayList(
        root = root,
        replacements = replacements + updates,
    )

    companion object {
        fun replacing(
            source: List<ConversationTurnUiModel>,
            updates: Map<Int, ConversationTurnUiModel>,
        ): ConversationTurnOverlayList = when (source) {
            is ConversationTurnOverlayList -> source.replacing(updates)
            else -> ConversationTurnOverlayList(source, updates)
        }
    }
}

internal fun buildConversationTurnProjectionIndex(
    baseline: ConversationTurnProjectionBaseline,
    realtime: RealtimeSessionProjection?,
): ConversationTurnProjectionIndex {
    val timeline = realtime?.timeline.orEmpty()
    val analysis = timeline.analyzeLiveTurnRanges()
    val timelineKeys = timeline.map(SessionTimelineItem::key)
    val absorbed = baseline.historicalTail?.canAcceptAssistant() == true &&
        analysis.ranges.firstOrNull()?.let { range ->
            timeline[range.startIndex] !is SessionTimelineItem.User
        } == true
    val liveTurnOrdinalByTimelineIndex = IntArray(timeline.size) { -1 }
    analysis.ranges.forEachIndexed { ordinal, range ->
        for (index in range.startIndex until range.endExclusive) {
            liveTurnOrdinalByTimelineIndex[index] = ordinal
        }
    }
    return ConversationTurnProjectionIndex(
        runtimeSessionId = realtime?.runtimeSessionId,
        connectionEpoch = realtime?.connectionEpoch,
        timelineKeys = timelineKeys,
        timelineItems = timeline,
        liveTurns = analysis.ranges,
        liveTurnOrdinalByTimelineIndex = liveTurnOrdinalByTimelineIndex,
        historicalTailAbsorbed = absorbed,
        hasUnambiguousBoundaries = analysis.unambiguous &&
            timelineKeys.toSet().size == timeline.size,
    )
}

internal fun projectConversationTurnsIncrementally(
    baseline: ConversationTurnProjectionBaseline,
    previousIndex: ConversationTurnProjectionIndex,
    previousRealtime: RealtimeSessionProjection,
    previousTurns: List<ConversationTurnUiModel>,
    realtime: RealtimeSessionProjection,
    onTimelineItemVisit: (SessionTimelineItem) -> Unit,
): IncrementalConversationTurnProjection? {
    if (
        previousIndex.runtimeSessionId != realtime.runtimeSessionId ||
        previousIndex.connectionEpoch != realtime.connectionEpoch
    ) return null

    val previousItems = previousIndex.timelineItems
    val currentItems = realtime.timeline
    val mutation = realtime.timelineMutation
    val changedIndex = mutation.firstChangedIndex
    val hasSourceBoundReplacement =
        mutation.sourceTimeline === previousItems &&
            currentItems.size == previousItems.size &&
            when {
                changedIndex == null -> currentItems === previousItems
                changedIndex !in currentItems.indices -> false
                previousItems[changedIndex].key != currentItems[changedIndex].key -> false
                else -> previousItems[changedIndex]
                    .hasSameTurnBoundaryIdentity(currentItems[changedIndex])
            }
    val currentIndex = if (hasSourceBoundReplacement) {
        previousIndex.copy(timelineItems = currentItems)
    } else {
        buildConversationTurnProjectionIndex(baseline, realtime)
    }
    if (!previousIndex.hasUnambiguousBoundaries || !currentIndex.hasUnambiguousBoundaries) return null

    val affectedTimelineIndices = linkedSetOf<Int>()
    if (hasSourceBoundReplacement) {
        if (
            changedIndex != null &&
            previousItems[changedIndex] !== currentItems[changedIndex] &&
            previousItems[changedIndex] != currentItems[changedIndex]
        ) {
            affectedTimelineIndices += changedIndex
        }
    } else {
        if (currentItems.size < previousItems.size) return null
        if (previousIndex.timelineKeys.indices.any { index ->
                currentIndex.timelineKeys.getOrNull(index) != previousIndex.timelineKeys[index]
            }
        ) return null
        previousItems.indices.forEach { index ->
            val previous = previousItems[index]
            val current = currentItems[index]
            if (previous !== current && previous != current) affectedTimelineIndices += index
        }
        if (currentItems.size > previousItems.size) {
            affectedTimelineIndices += previousItems.size
        }
    }

    val externallyChangedTurnKeys = realtimeProcessChanges(
        previous = previousRealtime,
        current = realtime,
    )
    val externallyAffectedOrdinals = if (externallyChangedTurnKeys.isEmpty()) {
        emptySet()
    } else {
        currentIndex.liveTurns.indices.filterTo(linkedSetOf()) { ordinal ->
            currentIndex.liveTurns[ordinal].sourceAssistantTurnKey in externallyChangedTurnKeys
        }
    }

    if (affectedTimelineIndices.isEmpty() && externallyAffectedOrdinals.isEmpty()) {
        return IncrementalConversationTurnProjection(previousTurns, currentIndex)
    }

    val affectedOrdinals = linkedSetOf<Int>()
    affectedTimelineIndices.forEach { timelineIndex ->
        val ordinal = currentIndex.liveTurnOrdinalByTimelineIndex.getOrNull(timelineIndex) ?: -1
        if (ordinal < 0) return null
        affectedOrdinals += ordinal
    }
    affectedOrdinals += externallyAffectedOrdinals

    if (currentItems.size == previousItems.size) {
        if (
            !hasSourceBoundReplacement &&
            !previousIndex.liveTurns.sameBoundariesAs(currentIndex.liveTurns)
        ) return null
    } else {
        val appendedAt = previousItems.size
        val appendedOrdinal = currentIndex.liveTurns.indexOfFirst { range ->
            appendedAt in range.startIndex until range.endExclusive
        }
        if (appendedOrdinal < 0) return null
        if (appendedOrdinal < previousIndex.liveTurns.lastIndex) return null
        for (ordinal in 0 until appendedOrdinal) {
            if (previousIndex.liveTurns.getOrNull(ordinal) != currentIndex.liveTurns.getOrNull(ordinal)) {
                return null
            }
        }
        if (appendedOrdinal < previousIndex.liveTurns.size) {
            val previousRange = previousIndex.liveTurns[appendedOrdinal]
            val currentRange = currentIndex.liveTurns[appendedOrdinal]
            val appendedAssistant = currentItems.getOrNull(appendedAt) as? SessionTimelineItem.AssistantTurn
            val pendingUserGainedFirstAssistant =
                previousRange.sourceAssistantTurnKey == null &&
                    appendedAssistant?.turnKey == currentRange.sourceAssistantTurnKey
            if (
                previousRange.startIndex != currentRange.startIndex ||
                previousRange.key != currentRange.key ||
                (
                    previousRange.sourceAssistantTurnKey != currentRange.sourceAssistantTurnKey &&
                        !pendingUserGainedFirstAssistant
                    )
            ) return null
        }
        (appendedOrdinal until currentIndex.liveTurns.size).forEach(affectedOrdinals::add)
    }

    if (
        previousIndex.liveTurns.isNotEmpty() &&
        previousIndex.historicalTailAbsorbed != currentIndex.historicalTailAbsorbed
    ) return null

    val expectedSize = baseline.historicalTurns.size + currentIndex.liveTurns.size -
        if (currentIndex.historicalTailAbsorbed) 1 else 0
    val canOverlayPreviousTurns = previousIndex.liveTurns.isNotEmpty() &&
        previousTurns.size == expectedSize
    val mutableResult = if (canOverlayPreviousTurns) {
        null
    } else if (previousIndex.liveTurns.isEmpty()) {
        baseline.historicalTurns.toMutableList()
    } else {
        previousTurns.toMutableList()
    }
    val replacements = linkedMapOf<Int, ConversationTurnUiModel>()
    affectedOrdinals.sorted().forEach { ordinal ->
        val range = currentIndex.liveTurns[ordinal]
        val liveTurn = currentItems
            .subList(range.startIndex, range.endExclusive)
            .toLiveTurns(onTimelineItemVisit)
            .singleOrNull()
            ?: return null
        liveTurn.attachRealtimeProcess(realtime)
        val projected = if (ordinal == 0 && currentIndex.historicalTailAbsorbed) {
            val historicalTail = baseline.historicalTail ?: return null
            historicalTail.toMutable().apply { absorb(liveTurn) }.toUiModel()
        } else {
            liveTurn.toUiModel()
        }
        val outputIndex = baseline.historicalTurns.size + ordinal -
            if (currentIndex.historicalTailAbsorbed) 1 else 0
        if (canOverlayPreviousTurns) {
            if (outputIndex !in previousTurns.indices) return null
            replacements[outputIndex] = projected
        } else {
            val result = requireNotNull(mutableResult)
            when {
                outputIndex < 0 || outputIndex > result.size -> return null
                outputIndex == result.size -> result += projected
                else -> result[outputIndex] = projected
            }
        }
    }
    val result: List<ConversationTurnUiModel> = if (canOverlayPreviousTurns) {
        ConversationTurnOverlayList.replacing(previousTurns, replacements)
    } else {
        requireNotNull(mutableResult)
    }
    if (result.size != expectedSize) return null
    return IncrementalConversationTurnProjection(result, currentIndex)
}

private data class LiveTurnRangeAnalysis(
    val ranges: List<ConversationLiveTurnRange>,
    val unambiguous: Boolean,
)

private data class LiveTurnRangeCursor(
    val startIndex: Int,
    val key: String,
    var sourceAssistantTurnKey: String? = null,
    var hasUserPrompt: Boolean = false,
    var canAcceptAssistant: Boolean = false,
)

private fun List<SessionTimelineItem>.analyzeLiveTurnRanges(): LiveTurnRangeAnalysis {
    val ranges = mutableListOf<ConversationLiveTurnRange>()
    var current: LiveTurnRangeCursor? = null
    var unambiguous = true

    fun finish(endExclusive: Int) {
        val cursor = current ?: return
        ranges += ConversationLiveTurnRange(
            startIndex = cursor.startIndex,
            endExclusive = endExclusive,
            key = cursor.key,
            sourceAssistantTurnKey = cursor.sourceAssistantTurnKey,
        )
        current = null
    }

    forEachIndexed { index, item ->
        when (item) {
            is SessionTimelineItem.User -> {
                finish(index)
                current = LiveTurnRangeCursor(
                    startIndex = index,
                    key = "turn:${item.key}",
                    hasUserPrompt = true,
                    canAcceptAssistant = true,
                )
            }
            is SessionTimelineItem.AssistantTurn -> {
                val sameAgentTurn = current?.sourceAssistantTurnKey == item.turnKey
                val pendingUserTurn = current?.canAcceptAssistant == true
                if (!sameAgentTurn && !pendingUserTurn) {
                    finish(index)
                    current = LiveTurnRangeCursor(index, "turn:${item.key}")
                }
                val cursor = requireNotNull(current)
                cursor.sourceAssistantTurnKey = item.turnKey
                cursor.canAcceptAssistant = false
            }
            is SessionTimelineItem.ProcessActivity -> {
                val cursor = current
                if (cursor == null) {
                    current = LiveTurnRangeCursor(
                        startIndex = index,
                        key = "turn:${item.turnKey}",
                        sourceAssistantTurnKey = item.turnKey,
                    )
                } else if (cursor.sourceAssistantTurnKey != item.turnKey) {
                    // The legacy projector replaces this cursor without flushing it. Do not
                    // incrementally reason across that lossy boundary.
                    unambiguous = false
                }
            }
            is SessionTimelineItem.ToolActivity,
            is SessionTimelineItem.ToolResultActivity,
            is SessionTimelineItem.StatusActivity,
            -> if (current == null) {
                current = LiveTurnRangeCursor(index, "turn:${item.key}")
            }
        }
    }
    finish(size)
    return LiveTurnRangeAnalysis(ranges, unambiguous)
}

private fun SessionTimelineItem.hasSameTurnBoundaryIdentity(
    other: SessionTimelineItem,
): Boolean = when {
    this::class != other::class -> false
    this is SessionTimelineItem.AssistantTurn && other is SessionTimelineItem.AssistantTurn ->
        turnKey == other.turnKey
    this is SessionTimelineItem.ProcessActivity && other is SessionTimelineItem.ProcessActivity ->
        turnKey == other.turnKey
    else -> true
}

private fun List<ConversationLiveTurnRange>.sameBoundariesAs(
    other: List<ConversationLiveTurnRange>,
): Boolean = size == other.size && indices.all { index ->
    val left = this[index]
    val right = other[index]
    left.startIndex == right.startIndex &&
        left.endExclusive == right.endExclusive &&
        left.key == right.key &&
        left.sourceAssistantTurnKey == right.sourceAssistantTurnKey
}

private fun realtimeProcessChanges(
    previous: RealtimeSessionProjection,
    current: RealtimeSessionProjection,
): Set<String> {
    if (
        previous.todoSections === current.todoSections &&
        previous.subagents === current.subagents &&
        previous.moaReferences === current.moaReferences &&
        previous.moaProgress === current.moaProgress
    ) return emptySet()

    val previousTodos = previous.todoSections.groupBy(LiveTodoSectionProjection::turnKey)
    val currentTodos = current.todoSections.groupBy(LiveTodoSectionProjection::turnKey)
    val previousSubagents = previous.subagents.groupBy(LiveSubagentProjection::turnKey)
    val currentSubagents = current.subagents.groupBy(LiveSubagentProjection::turnKey)
    val previousReferences = previous.moaReferences.groupBy(LiveMoaReferenceProjection::turnKey)
    val currentReferences = current.moaReferences.groupBy(LiveMoaReferenceProjection::turnKey)
    val previousProgress = previous.moaProgress.groupBy(LiveMoaProgressProjection::turnKey)
    val currentProgress = current.moaProgress.groupBy(LiveMoaProgressProjection::turnKey)
    val turnKeys = buildSet {
        addAll(previousTodos.keys)
        addAll(currentTodos.keys)
        addAll(previousSubagents.keys)
        addAll(currentSubagents.keys)
        addAll(previousReferences.keys)
        addAll(currentReferences.keys)
        addAll(previousProgress.keys)
        addAll(currentProgress.keys)
    }
    return turnKeys.filterTo(linkedSetOf()) { turnKey ->
        previousTodos[turnKey].orEmpty() != currentTodos[turnKey].orEmpty() ||
            previousSubagents[turnKey].orEmpty() != currentSubagents[turnKey].orEmpty() ||
            previousReferences[turnKey].orEmpty() != currentReferences[turnKey].orEmpty() ||
            previousProgress[turnKey].orEmpty() != currentProgress[turnKey].orEmpty()
    }
}

private fun SessionTranscript.toHistoricalTurns(): MutableList<MutableConversationTurn> {
    val turns = mutableListOf<MutableConversationTurn>()
    var current: MutableConversationTurn? = null

    messages.forEachIndexed { index, message ->
        if (message.displayKind == "hidden") return@forEachIndexed
        message.eventText()?.let { eventText ->
            current?.let(turns::add)
            current = null
            turns += MutableConversationTurn(
                key = "turn:event:${message.messageId ?: index}",
                eventText = eventText,
            )
            return@forEachIndexed
        }
        message.standaloneRole()?.let { standaloneRole ->
            current?.let(turns::add)
            current = null
            val text = message.content.displayText()
            turns += if (standaloneRole == HistoricalStandaloneRole.ERROR) {
                MutableConversationTurn(
                    key = "turn:error:${message.messageId ?: index}",
                    explicitStatus = ConversationTurnStatus.ERROR,
                    error = text,
                )
            } else {
                MutableConversationTurn(
                    key = "turn:event:${message.messageId ?: index}",
                    eventText = text,
                )
            }
            return@forEachIndexed
        }
        when (message.role.lowercase()) {
            "user" -> {
                current?.let(turns::add)
                current = MutableConversationTurn(
                    key = "turn:message:${message.messageId ?: index}",
                    userPrompt = ConversationPromptUiModel(
                        key = "message:${message.messageId ?: index}",
                        text = message.content.displayText(),
                    ),
                )
            }
            "assistant" -> {
                val turn = current ?: MutableConversationTurn(
                    key = "turn:message:${message.messageId ?: index}",
                ).also { current = it }
                turn.addAssistant(message)
            }
            "tool" -> {
                val turn = current ?: MutableConversationTurn(
                    key = "turn:message:${message.messageId ?: index}",
                ).also { current = it }
                turn.addToolResult(
                    message = message,
                    messageIdentity = message.messageId?.toString() ?: index.toString(),
                )
            }
        }
    }
    current?.let(turns::add)
    return turns
}

private fun List<SessionTimelineItem>.toLiveTurns(
    onTimelineItemVisit: (SessionTimelineItem) -> Unit = {},
): List<MutableConversationTurn> {
    val turns = mutableListOf<MutableConversationTurn>()
    var current: MutableConversationTurn? = null

    forEach { item ->
        onTimelineItemVisit(item)
        when (item) {
            is SessionTimelineItem.User -> {
                current?.let(turns::add)
                current = MutableConversationTurn(
                    key = "turn:${item.key}",
                    userPrompt = ConversationPromptUiModel(item.key, item.text),
                )
            }
            is SessionTimelineItem.AssistantTurn -> {
                val sameAgentTurn = current?.takeIf { it.sourceAssistantTurnKey == item.turnKey }
                val pendingUserTurn = current?.takeIf { it.canAcceptAssistant() }
                if (sameAgentTurn != null) {
                    sameAgentTurn.addAssistant(item)
                } else if (pendingUserTurn != null) {
                    pendingUserTurn.addAssistant(item)
                } else {
                    current?.let(turns::add)
                    current = MutableConversationTurn(key = "turn:${item.key}").also {
                        it.addAssistant(item)
                    }
                }
            }
            is SessionTimelineItem.ToolActivity -> {
                val turn = current ?: MutableConversationTurn(
                    key = "turn:${item.key}",
                ).also { current = it }
                turn.addTool(item)
            }
            is SessionTimelineItem.ToolResultActivity -> {
                val turn = current ?: MutableConversationTurn(
                    key = "turn:${item.key}",
                ).also { current = it }
                turn.addToolResultMarker(item)
            }
            is SessionTimelineItem.ProcessActivity -> {
                val turn = current?.takeIf { it.sourceAssistantTurnKey == item.turnKey }
                    ?: MutableConversationTurn(key = "turn:${item.turnKey}").also { current = it }
                turn.addProcessMarker(item)
            }
            is SessionTimelineItem.StatusActivity -> {
                val turn = current ?: MutableConversationTurn(
                    key = "turn:${item.key}",
                ).also { current = it }
                turn.addStatus(item)
            }
        }
    }
    current?.let(turns::add)
    return turns
}

internal sealed interface ConversationSectionSlot {
    val key: String

    data class Thinking(
        override val key: String,
        val text: String,
    ) : ConversationSectionSlot

    data class ToolGroup(
        override val key: String,
        val toolIds: List<String>,
    ) : ConversationSectionSlot

    data class Activity(
        override val key: String,
        val text: String,
        val tone: HermesConversationActivityTone,
    ) : ConversationSectionSlot

    data class Response(
        override val key: String,
        val text: String,
    ) : ConversationSectionSlot

    data class Diff(
        override val key: String,
        val diffKey: String,
    ) : ConversationSectionSlot

    data object Todo : ConversationSectionSlot {
        override val key: String = "todo"
    }

    data object Subagents : ConversationSectionSlot {
        override val key: String = "subagents"
    }
}

internal data class HistoricalConversationTurnSnapshot(
    val key: String,
    val sourceAssistantTurnKey: String?,
    val userPrompt: ConversationPromptUiModel?,
    val eventText: String?,
    val thinkingParts: List<String>,
    val statusTextParts: List<String>,
    val activityTone: HermesConversationActivityTone,
    val todoItems: List<HermesConversationTodoItem>,
    val tools: List<ConversationToolUiModel>,
    val subagents: List<HermesConversationSubagent>,
    val moaReferences: List<HermesConversationMoaReference>,
    val moaProgress: HermesConversationMoaProgress?,
    val diffs: Map<String, String>,
    val responseParts: List<String>,
    val sectionSlots: List<ConversationSectionSlot>,
    val explicitStatus: ConversationTurnStatus?,
    val error: String?,
) {
    fun canAcceptAssistant(): Boolean =
        userPrompt != null && responseParts.isEmpty() && explicitStatus == null
}

private data class MutableConversationTurn(
    var key: String,
    var sourceAssistantTurnKey: String? = null,
    var userPrompt: ConversationPromptUiModel? = null,
    var eventText: String? = null,
    val thinkingParts: MutableList<String> = mutableListOf(),
    val statusTextParts: MutableList<String> = mutableListOf(),
    var activityTone: HermesConversationActivityTone = HermesConversationActivityTone.INFO,
    var todoItems: List<HermesConversationTodoItem> = emptyList(),
    val toolTodoItems: MutableMap<String, List<HermesConversationTodoItem>> = linkedMapOf(),
    val tools: MutableList<ConversationToolUiModel> = mutableListOf(),
    val subagents: MutableList<HermesConversationSubagent> = mutableListOf(),
    val moaReferences: MutableList<HermesConversationMoaReference> = mutableListOf(),
    var moaProgress: HermesConversationMoaProgress? = null,
    val diffs: MutableMap<String, String> = linkedMapOf(),
    val responseParts: MutableList<String> = mutableListOf(),
    val sectionSlots: MutableList<ConversationSectionSlot> = mutableListOf(),
    var explicitStatus: ConversationTurnStatus? = null,
    var error: String? = null,
) {
    fun canAcceptAssistant(): Boolean =
        userPrompt != null && responseParts.isEmpty() && explicitStatus == null

    fun absorb(other: MutableConversationTurn) {
        if (userPrompt == null) userPrompt = other.userPrompt
        thinkingParts += other.thinkingParts
        statusTextParts += other.statusTextParts
        activityTone = maxOf(activityTone, other.activityTone)
        if (other.todoItems.isNotEmpty()) todoItems = other.todoItems
        sourceAssistantTurnKey = other.sourceAssistantTurnKey ?: sourceAssistantTurnKey
        other.tools.forEach { incoming ->
            val existingIndex = tools.indexOfFirst { it.toolId == incoming.toolId }
            if (existingIndex >= 0) {
                tools[existingIndex] = incoming
            } else {
                tools += incoming
            }
        }
        subagents += other.subagents
        moaReferences += other.moaReferences
        moaProgress = other.moaProgress ?: moaProgress
        diffs.putAll(other.diffs)
        responseParts += other.responseParts
        other.sectionSlots.forEach { incoming ->
            val existingIndex = sectionSlots.indexOfFirst { it.key == incoming.key }
            if (existingIndex >= 0) {
                sectionSlots[existingIndex] = incoming
            } else {
                sectionSlots += incoming
            }
        }
        explicitStatus = other.explicitStatus ?: explicitStatus
        error = other.error ?: error
    }

    fun addAssistant(message: SessionMessageProjection) {
        val sourceKey = "message:${message.messageId ?: key}"
        message.reasoningText().presentedConversationText()?.let { text ->
            thinkingParts += text
            sectionSlots += ConversationSectionSlot.Thinking("$sourceKey:thinking", text)
        }
        val calls = message.toolCalls as? JsonArray
        val text = message.content.displayText().presentedConversationText().orEmpty()
        if (text.isNotBlank()) {
            if (calls.isNullOrEmpty()) message.messageId?.let { key = "turn:response:$it" }
            responseParts += text
            sectionSlots += ConversationSectionSlot.Response("$sourceKey:response", text)
        }
        calls?.forEachIndexed { index, element ->
            val call = element as? JsonObject ?: return@forEachIndexed
            val function = call["function"] as? JsonObject
            val name = function?.string("name") ?: call.string("name")
            val arguments = listOf(
                function?.get("arguments"),
                call["arguments"],
                call["args"],
                call["input"],
                call["parameters"],
            ).firstOrNull { value ->
                HermesMessagePresentation.readableText(value).isNotBlank()
            }
            val presentation = HermesMessagePresentation.toolCall(name, arguments)
            val toolId = call.string("id")
                ?: call.string("call_id")
                ?: "message:${message.messageId ?: key}:tool:$index"
            addToolModel(ConversationToolUiModel(
                key = "tool:$toolId",
                toolId = toolId,
                name = name,
                callLabel = presentation.label,
                arguments = HermesMessagePresentation.readableText(arguments).takeIf(String::isNotBlank),
                argumentDetails = presentation.details,
                status = ConversationToolStatus.UNKNOWN,
            ))
        }
    }

    fun addAssistant(item: SessionTimelineItem.AssistantTurn) {
        sourceAssistantTurnKey = item.turnKey
        item.reasoning.presentedConversationText()?.let { text ->
            thinkingParts += text
            sectionSlots += ConversationSectionSlot.Thinking("${item.key}:reasoning", text)
        }
        item.thinking.presentedConversationText()?.let { text ->
            thinkingParts += text
            sectionSlots += ConversationSectionSlot.Thinking("${item.key}:thinking", text)
        }
        item.statusText.presentedToolText()?.let { text ->
            statusTextParts += text
            sectionSlots += ConversationSectionSlot.Activity(
                key = "${item.key}:activity",
                text = text,
                tone = HermesConversationActivityTone.INFO,
            )
        }
        item.text.presentedConversationText()?.let { text ->
            responseParts += text
            sectionSlots += ConversationSectionSlot.Response("${item.key}:response", text)
        }
        explicitStatus = when (item.status) {
            AssistantTurnStatus.STREAMING -> ConversationTurnStatus.STREAMING
            AssistantTurnStatus.COMPLETE -> ConversationTurnStatus.COMPLETE
            AssistantTurnStatus.ERROR -> ConversationTurnStatus.ERROR
        }
        error = item.error.presentedToolText()
    }

    fun attachRealtimeProcess(realtime: RealtimeSessionProjection) {
        val turnKey = sourceAssistantTurnKey ?: return
        val liveTodos = realtime.todoSections
            .asSequence()
            .filter { it.turnKey == turnKey }
            .sortedWith(compareBy(LiveTodoSectionProjection::firstEventSequence, LiveTodoSectionProjection::key))
            .flatMap { it.items.asSequence() }
            .map { item ->
                HermesConversationTodoItem(
                    key = item.key,
                    content = item.label,
                    status = item.status,
                )
            }
            .toList()
        if (liveTodos.isNotEmpty()) {
            todoItems = liveTodos
            if (ConversationSectionSlot.Todo !in sectionSlots) {
                sectionSlots += ConversationSectionSlot.Todo
            }
        }
        subagents += realtime.subagents
            .asSequence()
            .filter { it.turnKey == turnKey }
            .map { projection ->
                HermesConversationSubagent(
                    key = projection.key,
                    goal = projection.goal,
                    status = projection.status.toSectionStatus(),
                    parentKey = projection.parentKey,
                    model = projection.model,
                    summary = projection.summary,
                    durationSeconds = projection.durationSeconds,
                    taskIndex = projection.taskIndex,
                    taskCount = projection.taskCount,
                    tokenSummary = projection.tokenSummary(),
                    apiCalls = projection.apiCalls,
                )
            }
        moaReferences += realtime.moaReferences
            .asSequence()
            .filter { it.turnKey == turnKey }
            .map { projection ->
                HermesConversationMoaReference(
                    key = projection.key,
                    label = projection.label,
                    text = projection.text,
                )
            }
        moaProgress = realtime.moaProgress
            .firstOrNull { it.turnKey == turnKey }
            ?.let { projection ->
                HermesConversationMoaProgress(
                    phase = projection.phase,
                    aggregator = projection.aggregator,
                    refsDone = projection.refsDone,
                    refsTotal = projection.refsTotal,
                )
            }
        if (subagents.isNotEmpty() || moaReferences.isNotEmpty() || moaProgress != null) {
            val responseIndex = sectionSlots.indexOfLast { it is ConversationSectionSlot.Response }
            if (ConversationSectionSlot.Subagents !in sectionSlots) {
                if (responseIndex >= 0) {
                    sectionSlots.add(responseIndex, ConversationSectionSlot.Subagents)
                } else {
                    sectionSlots += ConversationSectionSlot.Subagents
                }
            }
        }
    }

    fun addProcessMarker(item: SessionTimelineItem.ProcessActivity) {
        sourceAssistantTurnKey = item.turnKey
        if (ConversationSectionSlot.Subagents !in sectionSlots) {
            sectionSlots += ConversationSectionSlot.Subagents
        }
    }

    fun addToolResult(
        message: SessionMessageProjection,
        messageIdentity: String,
    ) {
        val toolId = message.toolCallId ?: "message:$messageIdentity:tool-result"
        val index = tools.indexOfFirst { it.toolId == toolId }
        val existing = tools.getOrNull(index)
        val name = message.toolName ?: existing?.name
        val result = HermesMessagePresentation.toolOutput(message.content)
        val payload = HermesMessagePresentation.structuredObject(message.content)
        val replacement = (existing ?: ConversationToolUiModel(
            key = "tool:$toolId",
            toolId = toolId,
            name = message.toolName,
            status = ConversationToolStatus.UNKNOWN,
        )).copy(
            name = name,
            callLabel = existing?.callLabel
                ?: HermesMessagePresentation.toolCall(name, existing?.arguments).label,
            output = result.text,
            resultDetails = result.details,
            status = ConversationToolStatus.COMPLETE,
        )
        if (index >= 0) {
            tools[index] = replacement
        } else {
            addToolModel(replacement)
        }
        payload?.todoItems("tool:$toolId")?.let(::recordTodoItems)
        payload?.diffText()?.let { recordDiff("tool:$toolId", it) }
    }

    fun addTool(item: SessionTimelineItem.ToolActivity) {
        val argumentsElement = item.payload.firstPresentableElement(
            "arguments",
            "args",
            "input",
            "parameters",
        ) ?: item.args?.let(::JsonPrimitive)
        val payloadContext = item.payload.firstPresentableElement("context")
            ?.let(HermesMessagePresentation::payload)
            ?.visibleText()
            ?.takeIf(String::isNotBlank)
        val safeContext = payloadContext ?: item.context.presentedToolText()
        val call = HermesMessagePresentation.toolCall(
            name = item.name,
            arguments = argumentsElement,
            context = safeContext,
        )
        val output = HermesMessagePresentation.toolOutput(item.output)
        val result = HermesMessagePresentation.toolOutput(item.result)
        val payloadResult = HermesMessagePresentation.toolResult(
            item.payload,
            maxTextCodePoints = HermesMessagePresentation.MAX_LONG_OUTPUT_CODE_POINTS,
        )
        val safeArguments = HermesMessagePresentation.readableText(argumentsElement)
            .takeIf(String::isNotBlank)
        val safeSummary = item.summary.presentedToolText()
        val safeDiff = item.diff.presentedLongOutputText()
        val safeError = item.error.presentedToolText()
        val payloadHasOutput = item.payload.hasPresentableValue("output", "output_text")
        val payloadHasResult = item.payload.hasPresentableValue("result", "result_text")
        val payloadHasSummary = item.payload.hasPresentableValue("summary")
        val payloadHasDiff = item.payload.hasPresentableValue("diff", "inline_diff")
        val payloadHasError = item.payload.hasPresentableValue("error")
        val fallbackDetails = buildList {
            if (!payloadHasOutput) addAll(output.details)
            if (!payloadHasResult) addAll(result.details)
        }
        addToolModel(ConversationToolUiModel(
            key = item.key,
            toolId = item.toolId,
            name = item.name,
            callLabel = call.label,
            context = safeContext,
            arguments = safeArguments,
            argumentDetails = call.details,
            output = payloadResult.text.ifBlank { output.text },
            result = result.text.takeIf { !payloadHasResult && it.isNotBlank() },
            summary = safeSummary.takeUnless { payloadHasSummary },
            diff = safeDiff.takeUnless { payloadHasDiff },
            resultDetails = (payloadResult.details + fallbackDetails).distinct(),
            durationSeconds = item.durationSeconds,
            status = when (item.status) {
                ToolActivityStatus.RUNNING -> ConversationToolStatus.RUNNING
                ToolActivityStatus.COMPLETE -> ConversationToolStatus.COMPLETE
                ToolActivityStatus.ERROR -> ConversationToolStatus.ERROR
                ToolActivityStatus.INTERRUPTED -> ConversationToolStatus.INTERRUPTED
                ToolActivityStatus.UNKNOWN -> ConversationToolStatus.UNKNOWN
            },
            error = safeError.takeUnless { payloadHasError },
        ))
        item.payload.todoItems(item.key)?.let { toolTodoItems[item.key] = it }
        (item.payload.diffText() ?: safeDiff)?.let { diffs[item.key] = it }
    }

    fun addToolResultMarker(item: SessionTimelineItem.ToolResultActivity) {
        toolTodoItems[item.toolKey]?.let(::recordTodoItems)
        diffs[item.toolKey]?.let { recordDiff(item.toolKey, it) }
    }

    fun addStatus(item: SessionTimelineItem.StatusActivity) {
        val text = item.text.presentedToolText() ?: return
        val tone = item.kind.toActivityTone()
        statusTextParts += text
        activityTone = maxOf(activityTone, tone)
        sectionSlots += ConversationSectionSlot.Activity(item.key, text, tone)
    }

    private fun addToolModel(tool: ConversationToolUiModel) {
        tools += tool
        val last = sectionSlots.lastOrNull()
        if (last is ConversationSectionSlot.ToolGroup) {
            sectionSlots[sectionSlots.lastIndex] = last.copy(toolIds = last.toolIds + tool.toolId)
        } else {
            sectionSlots += ConversationSectionSlot.ToolGroup(
                key = tool.key,
                toolIds = listOf(tool.toolId),
            )
        }
    }

    private fun recordTodoItems(items: List<HermesConversationTodoItem>) {
        todoItems = items
        if (ConversationSectionSlot.Todo !in sectionSlots) {
            sectionSlots += ConversationSectionSlot.Todo
        }
    }

    private fun recordDiff(diffKey: String, text: String) {
        diffs[diffKey] = text
        val slot = ConversationSectionSlot.Diff("diff:$diffKey", diffKey)
        if (sectionSlots.none { it.key == slot.key }) {
            sectionSlots += slot
        }
    }

    fun toSnapshot(): HistoricalConversationTurnSnapshot = HistoricalConversationTurnSnapshot(
        key = key,
        sourceAssistantTurnKey = sourceAssistantTurnKey,
        userPrompt = userPrompt,
        eventText = eventText,
        thinkingParts = thinkingParts.toList(),
        statusTextParts = statusTextParts.toList(),
        activityTone = activityTone,
        todoItems = todoItems.toList(),
        tools = tools.toList(),
        subagents = subagents.toList(),
        moaReferences = moaReferences.toList(),
        moaProgress = moaProgress,
        diffs = diffs.toMap(),
        responseParts = responseParts.toList(),
        sectionSlots = sectionSlots.toList(),
        explicitStatus = explicitStatus,
        error = error,
    )

    fun toUiModel(): ConversationTurnUiModel {
        val thinking = thinkingParts.joinToString("\n\n")
        val statusText = statusTextParts.joinToString("\n")
        val tools = tools.toList()
        val todoItems = todoItems.toList()
        val subagents = subagents.toList()
        val moaReferences = moaReferences.toList()
        val response = responseParts.joinToString("\n\n")
        val status = explicitStatus ?: when {
            tools.any { it.status == ConversationToolStatus.ERROR } -> ConversationTurnStatus.ERROR
            tools.any {
                it.status == ConversationToolStatus.RUNNING ||
                    it.status == ConversationToolStatus.INTERRUPTED ||
                    it.status == ConversationToolStatus.UNKNOWN
            } -> ConversationTurnStatus.INCOMPLETE
            responseParts.isNotEmpty() -> ConversationTurnStatus.COMPLETE
            else -> ConversationTurnStatus.INCOMPLETE
        }
        return ConversationTurnUiModel(
            key = key,
            userPrompt = userPrompt,
            thinking = thinking,
            statusText = statusText,
            tools = tools,
            response = response,
            status = status,
            error = error,
            eventText = eventText,
            sections = canonicalSections(status),
        )
    }

    private fun canonicalSections(
        status: ConversationTurnStatus,
    ): CanonicalConversationSections = CanonicalConversationSections.from(
        buildList {
            userPrompt?.let { prompt ->
                add(
                    HermesConversationSection.UserPrompt(
                        metadata = sectionMetadata(
                            suffix = "user-prompt",
                            status = HermesConversationSectionStatus.COMPLETE,
                        ),
                        prompt = prompt,
                    ),
                )
            }
            val finalResponseIndex = sectionSlots.indexOfLast {
                it is ConversationSectionSlot.Response
            }
            val hasProcessBeforeFinalResponse = finalResponseIndex > 0 && sectionSlots
                .subList(0, finalResponseIndex)
                .any(ConversationSectionSlot::isProcess)
            val hasProcessAfterFinalResponse = finalResponseIndex >= 0 && sectionSlots
                .subList(finalResponseIndex + 1, sectionSlots.size)
                .any(ConversationSectionSlot::isProcess)
            val hasStandaloneSectionAfterSlots =
                !eventText.isNullOrBlank() || !error.isNullOrBlank()
            val toolsById = tools.associateBy(ConversationToolUiModel::toolId)
            sectionSlots.forEachIndexed { index, slot ->
                if (
                    index == finalResponseIndex &&
                    hasProcessBeforeFinalResponse &&
                    !hasProcessAfterFinalResponse &&
                    !hasStandaloneSectionAfterSlots &&
                    slot is ConversationSectionSlot.Response
                ) {
                    add(
                        HermesConversationSection.ResponseBoundary(
                            metadata = sectionMetadata(
                                suffix = "response-boundary:${slot.key}",
                                status = status.toSectionStatus(),
                            ),
                        ),
                    )
                }
                val activeStatus = if (index == sectionSlots.lastIndex) {
                    status.toSectionStatus()
                } else {
                    HermesConversationSectionStatus.COMPLETE
                }
                when (slot) {
                    is ConversationSectionSlot.Thinking -> add(
                        HermesConversationSection.Thinking(
                            metadata = sectionMetadata("thinking:${slot.key}", activeStatus),
                            text = slot.text,
                        ),
                    )
                    is ConversationSectionSlot.ToolGroup -> {
                        val group = slot.toolIds.mapNotNull(toolsById::get)
                        if (group.isNotEmpty()) {
                            add(
                                HermesConversationSection.ToolGroup(
                                    metadata = sectionMetadata(
                                        "tools:${slot.key}",
                                        group.sectionStatus(),
                                    ),
                                    tools = group,
                                ),
                            )
                        }
                    }
                    is ConversationSectionSlot.Activity -> add(
                        HermesConversationSection.Activity(
                            metadata = sectionMetadata(
                                "activity:${slot.key}",
                                when (slot.tone) {
                                    HermesConversationActivityTone.INFO -> activeStatus
                                    HermesConversationActivityTone.WARNING ->
                                        HermesConversationSectionStatus.INTERRUPTED
                                    HermesConversationActivityTone.ERROR ->
                                        HermesConversationSectionStatus.ERROR
                                },
                            ),
                            text = slot.text,
                            tone = slot.tone,
                        ),
                    )
                    is ConversationSectionSlot.Response -> add(
                        HermesConversationSection.AssistantResponse(
                            metadata = sectionMetadata("response:${slot.key}", activeStatus),
                            text = slot.text,
                        ),
                    )
                    is ConversationSectionSlot.Diff -> diffs[slot.diffKey]?.let { text ->
                        add(
                            HermesConversationSection.Diff(
                                metadata = sectionMetadata(
                                    "diff:${slot.key}",
                                    HermesConversationSectionStatus.COMPLETE,
                                ),
                                text = text,
                            ),
                        )
                    }
                    ConversationSectionSlot.Todo -> if (todoItems.isNotEmpty()) {
                        add(
                            HermesConversationSection.Todo(
                                metadata = sectionMetadata(
                                    "todo",
                                    todoItems.todoSectionStatus(),
                                ),
                                items = todoItems,
                            ),
                        )
                    }
                    ConversationSectionSlot.Subagents -> if (
                        subagents.isNotEmpty() || moaReferences.isNotEmpty() || moaProgress != null
                    ) {
                        add(
                            HermesConversationSection.Subagents(
                                metadata = sectionMetadata(
                                    "subagents",
                                    processSectionStatus(subagents, moaProgress),
                                ),
                                subagents = subagents,
                                moaReferences = moaReferences,
                                moaProgress = moaProgress,
                            ),
                        )
                    }
                }
            }
            eventText?.takeIf(String::isNotBlank)?.let { text ->
                add(
                    HermesConversationSection.Event(
                        metadata = sectionMetadata(
                            suffix = "event",
                            status = HermesConversationSectionStatus.COMPLETE,
                        ),
                        text = text,
                    ),
                )
            }
            error?.takeIf(String::isNotBlank)?.let { message ->
                add(
                    HermesConversationSection.Error(
                        metadata = sectionMetadata(
                            suffix = "error",
                            status = HermesConversationSectionStatus.ERROR,
                        ),
                        message = message,
                    ),
                )
            }
        },
    )

    private fun sectionMetadata(
        suffix: String,
        status: HermesConversationSectionStatus,
    ) = HermesConversationSectionMetadata(
        key = "$key:$suffix",
        status = status,
    )
}

private fun ConversationSectionSlot.isProcess(): Boolean =
    this !is ConversationSectionSlot.Response

private fun HistoricalConversationTurnSnapshot.toMutable(): MutableConversationTurn =
    MutableConversationTurn(
        key = key,
        sourceAssistantTurnKey = sourceAssistantTurnKey,
        userPrompt = userPrompt,
        eventText = eventText,
        thinkingParts = thinkingParts.toMutableList(),
        statusTextParts = statusTextParts.toMutableList(),
        activityTone = activityTone,
        todoItems = todoItems.toList(),
        tools = tools.toMutableList(),
        subagents = subagents.toMutableList(),
        moaReferences = moaReferences.toMutableList(),
        moaProgress = moaProgress,
        diffs = LinkedHashMap(diffs),
        responseParts = responseParts.toMutableList(),
        sectionSlots = sectionSlots.toMutableList(),
        explicitStatus = explicitStatus,
        error = error,
    )

private fun String?.toActivityTone(): HermesConversationActivityTone = when (
    this?.trim()?.lowercase()
) {
    "error", "failed", "failure" -> HermesConversationActivityTone.ERROR
    "warn", "warning", "interrupted", "interrupt" -> HermesConversationActivityTone.WARNING
    else -> HermesConversationActivityTone.INFO
}

private fun ConversationTurnStatus.toSectionStatus(): HermesConversationSectionStatus = when (this) {
    ConversationTurnStatus.INCOMPLETE -> HermesConversationSectionStatus.INTERRUPTED
    ConversationTurnStatus.STREAMING -> HermesConversationSectionStatus.STREAMING
    ConversationTurnStatus.COMPLETE -> HermesConversationSectionStatus.COMPLETE
    ConversationTurnStatus.ERROR -> HermesConversationSectionStatus.ERROR
}

private fun LiveSubagentStatus.toSectionStatus(): HermesConversationSectionStatus = when (this) {
    LiveSubagentStatus.RUNNING -> HermesConversationSectionStatus.RUNNING
    LiveSubagentStatus.COMPLETE -> HermesConversationSectionStatus.COMPLETE
    LiveSubagentStatus.ERROR -> HermesConversationSectionStatus.ERROR
    LiveSubagentStatus.INTERRUPTED -> HermesConversationSectionStatus.INTERRUPTED
}

private fun LiveSubagentProjection.tokenSummary(): HermesConversationTokenSummary? {
    val parts = listOf(inputTokens, outputTokens, reasoningTokens)
    if (parts.all { it == null }) return null
    return HermesConversationTokenSummary(
        inputTokens = inputTokens,
        outputTokens = outputTokens,
        reasoningTokens = reasoningTokens,
        totalTokens = parts.filterNotNull().sum(),
    )
}

private fun processSectionStatus(
    subagents: List<HermesConversationSubagent>,
    moaProgress: HermesConversationMoaProgress?,
): HermesConversationSectionStatus = when {
    subagents.any { it.status == HermesConversationSectionStatus.ERROR } ->
        HermesConversationSectionStatus.ERROR
    subagents.any { it.status == HermesConversationSectionStatus.INTERRUPTED } ->
        HermesConversationSectionStatus.INTERRUPTED
    subagents.any {
        it.status == HermesConversationSectionStatus.RUNNING ||
            it.status == HermesConversationSectionStatus.PENDING ||
            it.status == HermesConversationSectionStatus.STREAMING
    } -> HermesConversationSectionStatus.RUNNING
    moaProgress != null && (
        moaProgress.refsDone == null ||
            moaProgress.refsTotal == null ||
            moaProgress.refsDone < moaProgress.refsTotal
    ) -> HermesConversationSectionStatus.RUNNING
    else -> HermesConversationSectionStatus.COMPLETE
}

private fun List<ConversationToolUiModel>.sectionStatus(): HermesConversationSectionStatus = when {
    any { it.status == ConversationToolStatus.ERROR } -> HermesConversationSectionStatus.ERROR
    any { it.status == ConversationToolStatus.RUNNING } -> HermesConversationSectionStatus.RUNNING
    any {
        it.status == ConversationToolStatus.INTERRUPTED ||
            it.status == ConversationToolStatus.UNKNOWN
    } -> HermesConversationSectionStatus.INTERRUPTED
    else -> HermesConversationSectionStatus.COMPLETE
}

private fun List<HermesConversationTodoItem>.todoSectionStatus(): HermesConversationSectionStatus = when {
    any { it.status == HermesConversationTodoStatus.IN_PROGRESS } ->
        HermesConversationSectionStatus.RUNNING
    any { it.status == HermesConversationTodoStatus.PENDING } ->
        HermesConversationSectionStatus.PENDING
    else -> HermesConversationSectionStatus.COMPLETE
}

private enum class HistoricalStandaloneRole {
    EVENT,
    ERROR,
}

private fun SessionMessageProjection.standaloneRole(): HistoricalStandaloneRole? = when (role.lowercase()) {
    "system", "note", "warn", "warning" -> HistoricalStandaloneRole.EVENT
    "error" -> HistoricalStandaloneRole.ERROR
    else -> null
}

private fun SessionMessageProjection.eventText(): String? = when (displayKind) {
    "model_switch" -> "model changed"
    "async_delegation_complete" -> {
        val count = ((displayMetadata as? JsonObject)?.get("task_count") as? JsonPrimitive)?.intOrNull
        when (count) {
            null -> "background agent work finished"
            1 -> "1 background agent finished"
            else -> "$count background agents finished"
        }
    }
    else -> null
}

private fun SessionMessageProjection.reasoningText(): String? =
    reasoning?.takeIf(String::isNotBlank)
        ?: reasoningContent?.takeIf(String::isNotBlank)

private fun JsonObject.string(key: String): String? =
    (this[key] as? JsonPrimitive)?.contentOrNull?.takeIf(String::isNotBlank)

private fun JsonObject.hasPresentableValue(vararg keys: String): Boolean = keys.any { key ->
    get(key)?.let(HermesMessagePresentation::readableText)?.isNotBlank() == true
}

private fun JsonObject.firstPresentableElement(vararg keys: String): JsonElement? =
    keys.firstNotNullOfOrNull { key ->
        get(key)?.takeIf { value ->
            HermesMessagePresentation.readableText(value).isNotBlank()
        }
    }

private fun JsonObject.todoItems(parentKey: String): List<HermesConversationTodoItem>? =
    (get("todos") as? JsonArray)?.mapIndexedNotNull { index, element ->
        val item = element as? JsonObject ?: return@mapIndexedNotNull null
        val content = item.string("content").presentedToolText()?.takeIf(String::isNotBlank)
            ?: return@mapIndexedNotNull null
        HermesConversationTodoItem(
            key = item.string("id") ?: "$parentKey:todo:$index",
            content = content,
            status = when (item.string("status")?.lowercase()) {
                "in_progress", "in-progress", "running" -> HermesConversationTodoStatus.IN_PROGRESS
                "completed", "complete", "done" -> HermesConversationTodoStatus.COMPLETED
                "cancelled", "canceled" -> HermesConversationTodoStatus.CANCELLED
                else -> HermesConversationTodoStatus.PENDING
            },
        )
    }

private fun JsonObject.diffText(): String? =
    firstPresentableElement("inline_diff", "diff")
        ?.let(HermesMessagePresentation::toolOutput)
        ?.visibleText()
        ?.takeIf(String::isNotBlank)

private fun String?.presentedToolText(): String? = this
    ?.takeIf(String::isNotBlank)
    ?.let(HermesMessagePresentation::payloadText)
    ?.visibleText()
    ?.takeIf(String::isNotBlank)

private fun String?.presentedLongOutputText(): String? = this
    ?.takeIf(String::isNotBlank)
    ?.let(HermesMessagePresentation::toolOutput)
    ?.visibleText()
    ?.takeIf(String::isNotBlank)

private fun String?.presentedConversationText(): String? =
    HermesMessagePresentation.safeText(
        value = this,
        maxCodePoints = HermesMessagePresentation.MAX_LONG_OUTPUT_CODE_POINTS,
    )

private fun JsonElement?.displayText(): String = when (this) {
    null -> ""
    is JsonPrimitive -> contentOrNull.orEmpty()
    is JsonArray -> joinToString("\n") { it.displayText() }
    is JsonObject -> HermesMessagePresentation.toolOutput(this).visibleText()
}
