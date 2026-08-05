package app.hermesmobile.sessions

import app.hermesmobile.protocol.gateway.GatewayEvent
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionTranscript
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.longOrNull

internal const val MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS = 128 * 1024
internal const val MAX_TRACKED_TOOL_OUTPUT_SEQUENCES = 4096
internal const val REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER =
    "\n… [additional tool output omitted on mobile]"

private val TOOL_OUTPUT_TRANSPORT_KEYS = setOf(
    "text",
    "delta",
    "output",
    "output_text",
    "content",
    "stream",
)

@JvmInline
value class ConnectionEpoch(val value: Long)

data class EventCursor(
    val connectionEpoch: Long,
    val ordinal: Long,
) {
    init {
        require(connectionEpoch >= 0) { "Connection epoch must not be negative." }
        require(ordinal > 0) { "Event ordinal must be positive." }
    }
}

enum class LiveMessageStatus {
    COMPLETE,
    ERROR,
}

data class LiveAssistantMessage(
    val text: String,
    val reasoning: String,
    val status: LiveMessageStatus,
)

enum class LiveToolStatus {
    RUNNING,
    COMPLETE,
    ERROR,
    INTERRUPTED,
    UNKNOWN,
}

data class LiveToolProjection(
    val key: String,
    val name: String?,
    val status: LiveToolStatus,
    val payload: JsonObject,
    val turnKey: String? = null,
    val revision: Long? = null,
    val firstEventSequence: Long? = null,
    val identity: V2LifecycleIdentity? = null,
)

data class V2LifecycleIdentity(
    val turnId: String,
    val entityId: String,
)

internal object V2LifecycleProjectionKey {
    fun encode(kind: String, identity: V2LifecycleIdentity): String =
        "v2|$kind|${identity.turnId.length}|${identity.turnId}|" +
            "${identity.entityId.length}|${identity.entityId}"

    fun decode(value: String): Pair<String, V2LifecycleIdentity>? {
        if (!value.startsWith("v2|")) return null
        var cursor = 3
        fun token(): String? {
            val end = value.indexOf('|', cursor).takeIf { it >= cursor } ?: return null
            return value.substring(cursor, end).also { cursor = end + 1 }
        }
        val kind = token()?.takeIf(String::isNotEmpty) ?: return null
        val turnLength = token()?.toIntOrNull()?.takeIf { it >= 0 } ?: return null
        if (turnLength > value.length - cursor) return null
        val turnEnd = cursor + turnLength
        if (turnEnd > value.length || value.getOrNull(turnEnd) != '|') return null
        val turnId = value.substring(cursor, turnEnd)
        cursor = turnEnd + 1
        val entityLength = token()?.toIntOrNull()?.takeIf { it >= 0 } ?: return null
        if (entityLength > value.length - cursor) return null
        val entityEnd = cursor + entityLength
        if (entityEnd != value.length) return null
        return kind to V2LifecycleIdentity(turnId, value.substring(cursor, entityEnd))
    }
}

data class LiveTodoItemProjection(
    val key: String,
    val label: String,
    val status: HermesConversationTodoStatus,
)

data class LiveTodoSectionProjection(
    val key: String,
    val turnKey: String,
    val revision: Long,
    val firstEventSequence: Long,
    val status: HermesConversationTodoStatus,
    val items: List<LiveTodoItemProjection>,
    val identity: V2LifecycleIdentity? = null,
)

enum class LiveSubagentStatus {
    RUNNING,
    COMPLETE,
    ERROR,
    INTERRUPTED,
}

data class LiveSubagentProjection(
    val key: String,
    val turnKey: String,
    val parentKey: String? = null,
    val goal: String = "",
    val model: String? = null,
    val status: LiveSubagentStatus,
    val summary: String? = null,
    val durationSeconds: Double? = null,
    val taskIndex: Long? = null,
    val taskCount: Long? = null,
    val inputTokens: Long? = null,
    val outputTokens: Long? = null,
    val reasoningTokens: Long? = null,
    val apiCalls: Long? = null,
    val name: String = "Subagent",
    val revision: Long? = null,
    val firstEventSequence: Long? = null,
    val identity: V2LifecycleIdentity? = null,
    val parentIdentity: V2LifecycleIdentity? = null,
)

data class LiveTerminalProjection(
    val key: String,
    val turnKey: String,
    val revision: Long,
    val firstEventSequence: Long,
    val status: LiveToolStatus,
    val exitCode: Int? = null,
    val summary: String? = null,
    val durationSeconds: Double? = null,
    val identity: V2LifecycleIdentity? = null,
)

data class LiveMoaReferenceProjection(
    val key: String,
    val turnKey: String,
    val label: String,
    val text: String,
    val index: Int? = null,
    val count: Int? = null,
)

data class LiveMoaProgressProjection(
    val turnKey: String,
    val phase: String? = null,
    val aggregator: String? = null,
    val refsDone: Int? = null,
    val refsTotal: Int? = null,
)

enum class PendingInputKind {
    APPROVAL,
    CLARIFY,
    SECRET,
    SUDO,
}

data class PendingInput(
    val kind: PendingInputKind,
    val payload: JsonObject,
)

enum class PromptDeliveryState {
    QUEUED_LOCALLY,
    ACCEPTED_BY_GATEWAY,
    REJECTED,
}

data class OutboundPrompt(
    val clientPromptId: String,
    val text: String,
    val deliveryState: PromptDeliveryState,
)

data class RealtimeTimelineMutation(
    val sourceTimeline: List<SessionTimelineItem>? = null,
    val firstChangedIndex: Int? = null,
) {
    companion object {
        val Unknown = RealtimeTimelineMutation()
    }
}

private class TimelineItemOverlayList private constructor(
    private val root: List<SessionTimelineItem>,
    private val replacements: Map<Int, SessionTimelineItem>,
    private val knownIndicesByKey: Map<String, Int>,
) : AbstractList<SessionTimelineItem>() {
    override val size: Int
        get() = root.size

    override fun get(index: Int): SessionTimelineItem = replacements[index] ?: root[index]

    fun knownIndexOf(key: String): Int? = knownIndicesByKey[key]

    private fun replacing(index: Int, item: SessionTimelineItem): TimelineItemOverlayList =
        TimelineItemOverlayList(
            root = root,
            replacements = replacements + (index to item),
            knownIndicesByKey = knownIndicesByKey + (item.key to index),
        )

    companion object {
        fun replacing(
            source: List<SessionTimelineItem>,
            index: Int,
            item: SessionTimelineItem,
        ): TimelineItemOverlayList = when (source) {
            is TimelineItemOverlayList -> source.replacing(index, item)
            else -> TimelineItemOverlayList(
                root = source,
                replacements = mapOf(index to item),
                knownIndicesByKey = mapOf(item.key to index),
            )
        }
    }
}

private fun List<SessionTimelineItem>.indexOfTimelineKey(key: String): Int =
    (this as? TimelineItemOverlayList)?.knownIndexOf(key)
        ?: indexOfFirst { item -> item.key == key }

data class RealtimeSessionProjection(
    val sessionKey: SessionKey,
    val lineageTip: SessionKey,
    val runtimeSessionId: RuntimeSessionId,
    val transcript: SessionTranscript,
    val connectionEpoch: Long,
    val lastEventOrdinal: Long,
    val running: Boolean,
    val streamingAssistantText: String,
    val streamingReasoningText: String,
    val liveMessages: List<LiveAssistantMessage>,
    val todoSections: List<LiveTodoSectionProjection>,
    val tools: List<LiveToolProjection>,
    val subagents: List<LiveSubagentProjection>,
    val terminals: List<LiveTerminalProjection>,
    val moaReferences: List<LiveMoaReferenceProjection>,
    val moaProgress: List<LiveMoaProgressProjection>,
    val timeline: List<SessionTimelineItem>,
    val activeAssistantTurnKey: String?,
    val pendingInput: PendingInput?,
    val outboundPrompts: List<OutboundPrompt>,
    val lastError: String?,
    val activeToolIds: Set<String> = emptySet(),
    val seenToolOutputSequences: Map<String, Set<Long>> = emptyMap(),
    val timelineMutation: RealtimeTimelineMutation = RealtimeTimelineMutation.Unknown,
    val observerContractVersion: Int = 1,
    val observerProfile: String? = null,
    val runtimeGeneration: String? = null,
    val seenObserverTransportDigests: Map<Long, String> = emptyMap(),
)

/**
 * Merges one runtime session's gateway events on top of an authoritative REST
 * transcript. A new WebSocket epoch is rejected until [resync] installs a new
 * REST baseline, preventing stale or replayed events from crossing reconnects.
 */
class RealtimeSessionReducer {
    fun seed(
        transcript: SessionTranscript,
        runtimeSessionId: RuntimeSessionId,
        connectionEpoch: Long,
    ): RealtimeSessionProjection {
        require(connectionEpoch >= 0) { "Connection epoch must not be negative." }
        return RealtimeSessionProjection(
            sessionKey = transcript.sessionKey,
            lineageTip = transcript.lineageTip,
            runtimeSessionId = runtimeSessionId,
            transcript = transcript,
            connectionEpoch = connectionEpoch,
            lastEventOrdinal = 0,
            running = false,
            streamingAssistantText = "",
            streamingReasoningText = "",
            liveMessages = emptyList(),
            todoSections = emptyList(),
            tools = emptyList(),
            subagents = emptyList(),
            terminals = emptyList(),
            moaReferences = emptyList(),
            moaProgress = emptyList(),
            timeline = emptyList(),
            activeAssistantTurnKey = null,
            pendingInput = null,
            outboundPrompts = emptyList(),
            lastError = null,
        )
    }

    fun resync(
        current: RealtimeSessionProjection,
        transcript: SessionTranscript,
        connectionEpoch: Long,
    ): RealtimeSessionProjection {
        require(transcript.sessionKey == current.sessionKey) {
            "REST resync must target the attached session key."
        }
        return seed(transcript, current.runtimeSessionId, connectionEpoch).copy(
            outboundPrompts = current.outboundPrompts,
        )
    }

    fun apply(
        current: RealtimeSessionProjection,
        event: GatewayEvent,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        if (cursor.connectionEpoch != current.connectionEpoch) return current
        if (cursor.ordinal <= current.lastEventOrdinal) return current
        if (event.runtimeSessionId != current.runtimeSessionId) return current

        if (event.type in V2_LIFECYCLE_EVENT_TYPES) {
            val payload = event.payload ?: return current
            if (!app.hermesmobile.protocol.gateway.CloudObserverEventContract.accepts(event.type, payload)) {
                return current
            }
            return when (event.type) {
                "todo.update" -> current.applyTodoUpdate(payload, cursor)
                "subagent.update" -> current.applySubagentUpdate(payload, cursor)
                "tool.update" -> current.applyToolUpdate(payload, cursor)
                "terminal.update" -> current.applyTerminalUpdate(payload, cursor)
                else -> current
            }
        }

        val advanced = current.copy(
            lastEventOrdinal = cursor.ordinal,
            timelineMutation = RealtimeTimelineMutation(sourceTimeline = current.timeline),
        )
        val payload = event.payload ?: JsonObject(emptyMap())
        return when (event.type) {
            "message.start" -> {
                val turnKey = "assistant:${cursor.connectionEpoch}:${cursor.ordinal}"
                advanced.copy(
                    running = true,
                    streamingAssistantText = "",
                    streamingReasoningText = "",
                    timeline = advanced.timeline + SessionTimelineItem.AssistantTurn(key = turnKey),
                    timelineMutation = RealtimeTimelineMutation.Unknown,
                    activeAssistantTurnKey = turnKey,
                    pendingInput = null,
                    lastError = null,
                )
            }
            "message.delta" -> {
                val ensured = advanced.ensureAssistantTurn(cursor)
                ensured.copy(
                    streamingAssistantText = ensured.streamingAssistantText + payload.string("text"),
                ).appendAssistantText(payload.string("text"), cursor)
            }
            "reasoning.delta" -> {
                val delta = payload.string("text")
                val ensured = advanced.ensureAssistantTurn(cursor)
                ensured.copy(
                    streamingReasoningText = ensured.streamingReasoningText + delta,
                ).updateAssistantSegment(cursor, AssistantSegmentKind.REASONING) {
                    it.copy(reasoning = it.reasoning + delta)
                }
            }
            "reasoning.available" -> {
                val ensured = advanced.ensureAssistantTurn(cursor)
                val incoming = payload.string("text")
                val currentSegment = (ensured.timeline.lastOrNull() as? SessionTimelineItem.AssistantTurn)
                    ?.takeIf {
                        it.turnKey == ensured.activeAssistantTurnKey &&
                            it.segmentKind == AssistantSegmentKind.REASONING
                    }
                    ?.reasoning
                    .orEmpty()
                val reasoning = if (
                    currentSegment.isNotEmpty() &&
                    ensured.streamingReasoningText.endsWith(currentSegment)
                ) {
                    ensured.streamingReasoningText.removeSuffix(currentSegment) + when {
                        incoming.isEmpty() || currentSegment.startsWith(incoming) -> currentSegment
                        else -> incoming
                    }
                } else {
                    mergeStreamedText(ensured.streamingReasoningText, incoming)
                }
                ensured.copy(streamingReasoningText = reasoning)
                    .replaceAssistantReasoning(incoming, cursor)
            }
            "thinking.delta" -> {
                val delta = payload.string("text")
                advanced.ensureAssistantTurn(cursor)
                    .updateAssistantSegment(cursor, AssistantSegmentKind.THINKING) {
                        it.copy(thinking = it.thinking + delta)
                    }
            }
            "status.update" -> {
                val delta = payload.string("text")
                advanced.ensureAssistantTurn(cursor)
                    .updateAssistantSegment(cursor, AssistantSegmentKind.ACTIVITY) {
                        it.copy(statusText = it.statusText + delta)
                    }
            }
            "message.interim" -> {
                val ensured = advanced.ensureAssistantTurn(cursor)
                val mergedText = mergeStreamedText(
                    ensured.streamingAssistantText,
                    payload.string("text"),
                )
                ensured.copy(streamingAssistantText = mergedText)
                    .replaceAssistantText(mergedText, cursor)
            }
            "message.complete" -> {
                val error = payload.string("error").ifBlank { null }
                val failed = payload.string("status") == "error"
                val status = if (failed) LiveMessageStatus.ERROR else LiveMessageStatus.COMPLETE
                val completeText = payload.string("text")
                    .ifBlank { payload.string("rendered") }
                val text = completeText.ifBlank { advanced.streamingAssistantText }
                advanced.ensureAssistantTurn(cursor).sealMessage(
                    text = text,
                    status = status,
                    keepRunning = false,
                    error = error,
                ).sealAssistantTurn(
                    text = text,
                    status = if (failed) AssistantTurnStatus.ERROR else AssistantTurnStatus.COMPLETE,
                    error = error,
                    cursor = cursor,
                )
            }
            "tool.start", "tool.progress", "tool.generating" ->
                advanced.upsertTool(payload, ToolActivityStatus.RUNNING)
            "tool.output.delta" -> advanced.appendToolOutput(payload)
            "agent.terminal.output" -> advanced.appendTerminalOutput(payload)
            "agent.terminal.complete" -> advanced.completeTerminalOutput(payload)
            "tool.complete" -> advanced.upsertTool(payload, ToolActivityStatus.COMPLETE)
                .appendToolResultMarker(payload)
            "tool.error" -> advanced.upsertTool(payload, ToolActivityStatus.ERROR)
                .appendToolResultMarker(payload)
            "tool.interrupted" -> advanced.upsertTool(payload, ToolActivityStatus.INTERRUPTED)
                .appendToolResultMarker(payload)
            "subagent.start", "subagent.thinking", "subagent.tool" ->
                advanced.ensureAssistantTurn(cursor)
                    .upsertSubagent(payload, LiveSubagentStatus.RUNNING)
            "subagent.complete" ->
                advanced.ensureAssistantTurn(cursor)
                    .upsertSubagent(payload, payload.subagentCompletionStatus())
            "moa.reference" ->
                advanced.ensureAssistantTurn(cursor).upsertMoaReference(payload)
            "moa.aggregating", "moa.progress", "moa.phase" ->
                advanced.ensureAssistantTurn(cursor).upsertMoaProgress(event.type, payload)
            "approval.request", "clarify.request", "secret.request", "sudo.request",
            "approval.expire", "clarify.expire", "secret.expire", "sudo.expire",
            -> advanced // Interactive pending input is authoritative on the control plane only.
            "error" -> {
                val error = payload.string("message").ifBlank { "Hermes realtime error." }
                if (advanced.activeAssistantTurnKey == null) {
                    advanced.copy(
                        running = false,
                        pendingInput = null,
                        lastError = error,
                    )
                } else {
                    advanced.sealMessage(
                        text = advanced.streamingAssistantText,
                        status = LiveMessageStatus.ERROR,
                        keepRunning = false,
                        error = error,
                    ).sealAssistantTurn(
                        text = advanced.streamingAssistantText,
                        status = AssistantTurnStatus.ERROR,
                        error = error,
                        cursor = cursor,
                    )
                }
            }
            "session.info" -> advanced.copy(
                running = payload.boolean("running") ?: advanced.running,
            )
            else -> current
        }
    }

    fun markPromptQueued(
        current: RealtimeSessionProjection,
        clientPromptId: String,
        text: String,
    ): RealtimeSessionProjection {
        require(clientPromptId.isNotBlank()) { "Client prompt id is required." }
        require(text.isNotBlank()) { "Prompt text is required." }
        require(current.outboundPrompts.none { it.clientPromptId == clientPromptId }) {
            "Client prompt id is already present."
        }
        return current.copy(
            outboundPrompts = current.outboundPrompts + OutboundPrompt(
                clientPromptId = clientPromptId,
                text = text,
                deliveryState = PromptDeliveryState.QUEUED_LOCALLY,
            ),
        )
    }

    fun markPromptAccepted(
        current: RealtimeSessionProjection,
        clientPromptId: String,
    ): RealtimeSessionProjection = current.updatePrompt(clientPromptId) {
        it.copy(deliveryState = PromptDeliveryState.ACCEPTED_BY_GATEWAY)
    }

    fun markPromptRejected(
        current: RealtimeSessionProjection,
        clientPromptId: String,
    ): RealtimeSessionProjection = current.updatePrompt(clientPromptId) {
        it.copy(deliveryState = PromptDeliveryState.REJECTED)
    }

    private fun RealtimeSessionProjection.applyTodoUpdate(
        payload: JsonObject,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val turnId = payload.firstString("turn_id") ?: return this
        val sectionId = payload.firstString("section_id") ?: return this
        val identity = V2LifecycleIdentity(turnId, sectionId)
        val key = V2LifecycleProjectionKey.encode("todo", identity)
        val existing = todoSections.firstOrNull { it.identity == identity }
        val revision = payload.firstLong("revision") ?: return this
        val first = payload.firstLong("first_event_sequence") ?: return this
        if (!validLifecycleRevision(existing?.revision, existing?.firstEventSequence, revision, first, cursor)) {
            return this
        }
        if (payload.string("operation") == "delete") {
            if (
                existing == null ||
                existing.items.any { it.status !in TERMINAL_TODO_STATUSES }
            ) {
                return this
            }
            return copy(
                todoSections = todoSections.filterNot { it.identity == identity },
                lastEventOrdinal = cursor.ordinal,
                timelineMutation = RealtimeTimelineMutation.Unknown,
            )
        }
        val items = (payload["items"] as? JsonArray)?.map { raw ->
            val item = raw as? JsonObject ?: return this
            LiveTodoItemProjection(
                key = item.firstString("id") ?: return this,
                label = HermesMessagePresentation.safeText(item.firstString("label")) ?: "Task",
                status = item.firstString("status").toTodoStatus(),
            )
        } ?: return this
        val previousItems = existing?.items.orEmpty()
        if (
            items.size < previousItems.size ||
            previousItems.indices.any { index ->
                val previous = previousItems[index]
                val incoming = items[index]
                previous.key != incoming.key ||
                    previous.label != incoming.label ||
                    (previous.status in TERMINAL_TODO_STATUSES && previous.status != incoming.status)
            }
        ) {
            return this
        }
        val replacement = LiveTodoSectionProjection(
            key = key,
            turnKey = turnId,
            revision = revision,
            firstEventSequence = first,
            status = payload.firstString("status").toTodoStatus(),
            items = items,
            identity = identity,
        )
        return copy(
            todoSections = todoSections.upsertSorted(replacement, LiveTodoSectionProjection::key) {
                firstEventSequence to key
            },
            lastEventOrdinal = cursor.ordinal,
            timelineMutation = RealtimeTimelineMutation.Unknown,
        ).ensureProcessMarker(turnId)
    }

    private fun RealtimeSessionProjection.applySubagentUpdate(
        payload: JsonObject,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val turnId = payload.firstString("turn_id") ?: return this
        val entityId = payload.firstString("subagent_id") ?: return this
        val identity = V2LifecycleIdentity(turnId, entityId)
        val key = V2LifecycleProjectionKey.encode("subagent", identity)
        val existing = subagents.firstOrNull { it.identity == identity }
        val revision = payload.firstLong("revision") ?: return this
        val first = payload.firstLong("first_event_sequence") ?: return this
        if (!validLifecycleRevision(existing?.revision, existing?.firstEventSequence, revision, first, cursor)) {
            return this
        }
        if (payload.string("operation") == "delete") {
            if (
                existing == null ||
                existing.status !in TERMINAL_SUBAGENT_STATUSES ||
                subagents.any { it.parentIdentity == identity }
            ) {
                return this
            }
            return copy(
                subagents = subagents.filterNot { it.identity == identity },
                lastEventOrdinal = cursor.ordinal,
            )
        }
        if (existing == null && subagents.size >= MAX_V2_SUBAGENTS) return this
        val parentEntityId = (payload["parent_subagent_id"] as? JsonPrimitive)
            ?.takeIf { it.isString }
            ?.content
        val parentIdentity = parentEntityId?.let { V2LifecycleIdentity(turnId, it) }
        val parentKey = parentIdentity?.let { V2LifecycleProjectionKey.encode("subagent", it) }
        if (parentIdentity != null && subagents.none { it.identity == parentIdentity }) return this
        val status = payload.firstString("status").toSubagentStatus()
        if (existing?.let { it.status in TERMINAL_SUBAGENT_STATUSES && it.status != status } == true) return this
        val progress = payload["progress"] as? JsonObject
        val tokenCounts = payload["token_counts"] as? JsonObject
        val incomingGoal = HermesMessagePresentation.safeText(payload.string("goal")) ?: ""
        val incomingModel = HermesMessagePresentation.safeText(
            payload.firstString("model"),
            maxCodePoints = 160,
        )
        val incomingSummary = HermesMessagePresentation.safeText(payload.stringOrNull("summary"))
        val incomingDuration = payload.firstLong("duration_ms")?.div(1_000.0)
        val incomingTaskIndex = progress?.firstLong("current")
        val incomingTaskCount = progress?.firstLong("total")
        val incomingInputTokens = tokenCounts?.firstLong("input")
        val incomingOutputTokens = tokenCounts?.firstLong("output")
        val incomingReasoningTokens = tokenCounts?.firstLong("reasoning")
        val incomingApiCalls = payload.firstLong("api_calls")
        val incomingName = HermesMessagePresentation.safeText(
            payload.firstString("name"),
            maxCodePoints = 160,
        ) ?: "Subagent"
        if (
            existing?.status in TERMINAL_SUBAGENT_STATUSES &&
            existing != null &&
            (
                existing.parentIdentity != parentIdentity ||
                    (existing.goal.isNotEmpty() && existing.goal != incomingGoal) ||
                    existing.name != incomingName ||
                    !preservesExistingMetadata(existing.model, incomingModel, "model" in payload) ||
                    !preservesExistingMetadata(existing.summary, incomingSummary, "summary" in payload) ||
                    !preservesExistingMetadata(existing.durationSeconds, incomingDuration, "duration_ms" in payload) ||
                    !preservesExistingMetadata(existing.taskIndex, incomingTaskIndex, progress != null) ||
                    !preservesExistingMetadata(existing.taskCount, incomingTaskCount, progress != null) ||
                    !preservesExistingMetadata(existing.inputTokens, incomingInputTokens, tokenCounts != null) ||
                    !preservesExistingMetadata(existing.outputTokens, incomingOutputTokens, tokenCounts != null) ||
                    !preservesExistingMetadata(existing.reasoningTokens, incomingReasoningTokens, tokenCounts != null) ||
                    !preservesExistingMetadata(existing.apiCalls, incomingApiCalls, "api_calls" in payload)
            )
        ) {
            return this
        }
        val replacement = LiveSubagentProjection(
            key = key,
            turnKey = turnId,
            parentKey = parentKey,
            goal = incomingGoal.takeIf(String::isNotEmpty) ?: existing?.goal.orEmpty(),
            model = incomingModel ?: existing?.model,
            status = status,
            summary = incomingSummary ?: existing?.summary,
            durationSeconds = incomingDuration ?: existing?.durationSeconds,
            taskIndex = incomingTaskIndex ?: existing?.taskIndex,
            taskCount = incomingTaskCount ?: existing?.taskCount,
            inputTokens = incomingInputTokens ?: existing?.inputTokens,
            outputTokens = incomingOutputTokens ?: existing?.outputTokens,
            reasoningTokens = incomingReasoningTokens ?: existing?.reasoningTokens,
            apiCalls = incomingApiCalls ?: existing?.apiCalls,
            name = incomingName,
            revision = revision,
            firstEventSequence = first,
            identity = identity,
            parentIdentity = parentIdentity,
        )
        val proposed = subagents.upsertSorted(replacement, LiveSubagentProjection::key) {
            (firstEventSequence ?: Long.MAX_VALUE) to key
        }
        if (!proposed.isValidV2SubagentGraph()) return this
        return copy(
            subagents = proposed,
            lastEventOrdinal = cursor.ordinal,
        ).ensureProcessMarker(turnId)
    }

    private fun RealtimeSessionProjection.applyToolUpdate(
        payload: JsonObject,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val turnId = payload.firstString("turn_id") ?: return this
        val entityId = payload.firstString("tool_call_id") ?: return this
        val identity = V2LifecycleIdentity(turnId, entityId)
        val key = V2LifecycleProjectionKey.encode("tool", identity)
        val existing = tools.firstOrNull { it.identity == identity }
        val revision = payload.firstLong("revision") ?: return this
        val first = payload.firstLong("first_event_sequence") ?: return this
        if (!validLifecycleRevision(existing?.revision, existing?.firstEventSequence, revision, first, cursor)) {
            return this
        }
        if (payload.string("operation") == "delete") {
            if (existing == null || existing.status !in TERMINAL_TOOL_STATUSES) return this
            return copy(
                tools = tools.filterNot { it.identity == identity },
                activeToolIds = activeToolIds - key,
                timeline = timeline.filterNot { it.key == "tool:$key" || it.key == "tool:$key:result" },
                lastEventOrdinal = cursor.ordinal,
                timelineMutation = RealtimeTimelineMutation.Unknown,
            )
        }
        val status = payload.firstString("status").toToolStatus()
        if (existing?.let { it.status in TERMINAL_TOOL_STATUSES && it.status != status } == true) return this
        if (
            existing?.status in TERMINAL_TOOL_STATUSES &&
            existing != null &&
            !preservesExistingJsonMetadata(
                existing = existing.payload,
                incoming = payload,
                keys = TOOL_ABSORBING_METADATA_FIELDS,
            )
        ) {
            return this
        }
        val mapped = JsonObject(payload + ("tool_id" to JsonPrimitive(key)))
        val updated = upsertTool(mapped, status.toToolActivityStatus())
        return updated.copy(
            tools = updated.tools.map { tool ->
                if (tool.key == key) {
                    tool.copy(
                        turnKey = turnId,
                        revision = revision,
                        firstEventSequence = first,
                        identity = identity,
                    )
                } else {
                    tool
                }
            },
            lastEventOrdinal = cursor.ordinal,
        )
    }

    private fun RealtimeSessionProjection.applyTerminalUpdate(
        payload: JsonObject,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val turnId = payload.firstString("turn_id") ?: return this
        val entityId = payload.firstString("process_id") ?: return this
        val identity = V2LifecycleIdentity(turnId, entityId)
        val key = V2LifecycleProjectionKey.encode("terminal", identity)
        val existing = terminals.firstOrNull { it.identity == identity }
        val revision = payload.firstLong("revision") ?: return this
        val first = payload.firstLong("first_event_sequence") ?: return this
        if (!validLifecycleRevision(existing?.revision, existing?.firstEventSequence, revision, first, cursor)) {
            return this
        }
        if (payload.string("operation") == "delete") {
            if (existing == null || existing.status !in TERMINAL_TOOL_STATUSES) return this
            return copy(
                terminals = terminals.filterNot { it.identity == identity },
                tools = tools.filterNot { it.key == key },
                activeToolIds = activeToolIds - key,
                timeline = timeline.filterNot { it.key == "tool:$key" || it.key == "tool:$key:result" },
                lastEventOrdinal = cursor.ordinal,
                timelineMutation = RealtimeTimelineMutation.Unknown,
            )
        }
        val status = payload.firstString("status").toToolStatus()
        if (existing?.let { it.status in TERMINAL_TOOL_STATUSES && it.status != status } == true) return this
        val incomingExitCode = payload.firstLong("exit_code")?.toInt()
        val incomingSummary = HermesMessagePresentation.safeText(payload.firstString("summary"))
        val incomingDuration = payload.firstLong("duration_ms")?.div(1_000.0)
        if (
            existing?.status in TERMINAL_TOOL_STATUSES &&
            existing != null &&
            (
                !preservesExistingMetadata(existing.exitCode, incomingExitCode, "exit_code" in payload) ||
                    !preservesExistingMetadata(existing.summary, incomingSummary, "summary" in payload) ||
                    !preservesExistingMetadata(existing.durationSeconds, incomingDuration, "duration_ms" in payload)
            )
        ) {
            return this
        }
        val mapped = JsonObject(
            payload + mapOf(
                "tool_id" to JsonPrimitive(key),
                "name" to JsonPrimitive("terminal"),
            ),
        )
        val withTool = upsertTool(mapped, status.toToolActivityStatus())
        val replacement = LiveTerminalProjection(
            key = key,
            turnKey = turnId,
            revision = revision,
            firstEventSequence = first,
            status = status,
            exitCode = incomingExitCode ?: existing?.exitCode,
            summary = incomingSummary ?: existing?.summary,
            durationSeconds = incomingDuration ?: existing?.durationSeconds,
            identity = identity,
        )
        return withTool.copy(
            terminals = terminals.upsertSorted(replacement, LiveTerminalProjection::key) {
                firstEventSequence to key
            },
            tools = withTool.tools.filterNot { it.key == key },
            lastEventOrdinal = cursor.ordinal,
        )
    }

    private fun RealtimeSessionProjection.ensureAssistantTurn(
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        if (activeAssistantTurnKey != null) return this
        val turnKey = "assistant:${cursor.connectionEpoch}:${cursor.ordinal}"
        return copy(
            running = true,
            timeline = timeline + SessionTimelineItem.AssistantTurn(key = turnKey),
            timelineMutation = RealtimeTimelineMutation.Unknown,
            activeAssistantTurnKey = turnKey,
        )
    }

    private fun RealtimeSessionProjection.sealMessage(
        text: String,
        status: LiveMessageStatus,
        keepRunning: Boolean,
        error: String? = null,
    ): RealtimeSessionProjection {
        val sealed = LiveAssistantMessage(
            text = text,
            reasoning = streamingReasoningText,
            status = status,
        )
        val shouldAppend = text.isNotBlank() || streamingReasoningText.isNotBlank() || error != null
        return copy(
            running = keepRunning,
            streamingAssistantText = "",
            streamingReasoningText = "",
            liveMessages = if (shouldAppend) liveMessages + sealed else liveMessages,
            pendingInput = if (keepRunning) pendingInput else null,
            lastError = error,
        )
    }

    private fun RealtimeSessionProjection.upsertTool(
        payload: JsonObject,
        status: ToolActivityStatus,
    ): RealtimeSessionProjection {
        val toolId = payload.projectionToolId()
            ?: return this
        val key = "tool:$toolId"
        val existingActivityIndex = timeline.indexOfTimelineKey(key)
        val existingActivity = timeline.getOrNull(existingActivityIndex)
            as? SessionTimelineItem.ToolActivity
        val existingProjection = tools.firstOrNull { it.key == toolId }
        val name = payload.firstString("name", "tool_name")
            ?: existingActivity?.name
            ?: existingProjection?.name
        val existingOutput = existingActivity?.output.orEmpty()
        val output = boundedRealtimeToolOutput(
            mergeStreamedText(
                existingOutput,
                payload.firstString("output", "output_text").orEmpty(),
            ),
        )
        val mergedPayload = JsonObject(
            buildMap {
                existingActivity?.payload?.let(::putAll)
                existingProjection?.payload?.let(::putAll)
                payload.forEach { (key, value) ->
                    if (
                        key !in TOOL_OUTPUT_TRANSPORT_KEYS &&
                        (value.hasProtocolContent() || get(key)?.hasProtocolContent() != true)
                    ) {
                        put(key, value)
                    }
                }
                if (output.isNotBlank()) {
                    put("output", JsonPrimitive(output))
                }
            },
        )
        val activity = SessionTimelineItem.ToolActivity(
            key = key,
            toolId = toolId,
            name = name,
            context = payload.firstString("context") ?: existingActivity?.context,
            args = payload.firstString("args_text", "arguments", "args", "input")
                ?: existingActivity?.args,
            output = output,
            result = payload.firstString("result_text", "result") ?: existingActivity?.result,
            summary = payload.firstString("summary") ?: existingActivity?.summary,
            diff = payload.firstString("inline_diff", "diff") ?: existingActivity?.diff,
            durationSeconds = payload.firstDouble("duration_s", "duration_seconds")
                ?: existingActivity?.durationSeconds,
            status = status,
            error = payload.firstString("error") ?: existingActivity?.error,
            payload = mergedPayload,
        )
        val liveStatus = when (status) {
            ToolActivityStatus.RUNNING -> LiveToolStatus.RUNNING
            ToolActivityStatus.COMPLETE -> LiveToolStatus.COMPLETE
            ToolActivityStatus.ERROR -> LiveToolStatus.ERROR
            ToolActivityStatus.INTERRUPTED -> LiveToolStatus.INTERRUPTED
            ToolActivityStatus.UNKNOWN -> LiveToolStatus.UNKNOWN
        }
        val replacement = LiveToolProjection(toolId, name, liveStatus, mergedPayload)
        val updatedTools = if (existingProjection == null) {
            tools + replacement
        } else {
            tools.map { if (it.key == toolId) replacement else it }
        }
        val updatedActiveIds = if (status == ToolActivityStatus.RUNNING) {
            activeToolIds + toolId
        } else {
            activeToolIds - toolId
        }
        val withTimeline = if (existingActivity == null) {
            copy(
                timeline = timeline + activity,
                timelineMutation = RealtimeTimelineMutation.Unknown,
            )
        } else {
            replaceTimelineItemAt(existingActivityIndex, activity)
        }
        return withTimeline.copy(
            tools = updatedTools,
            activeToolIds = updatedActiveIds,
        )
    }

    private fun RealtimeSessionProjection.appendToolResultMarker(
        payload: JsonObject,
    ): RealtimeSessionProjection {
        val toolId = payload.projectionToolId() ?: return this
        val toolKey = "tool:$toolId"
        val markerKey = "$toolKey:result"
        if (timeline.any { it.key == markerKey }) return this
        return copy(
            timeline = timeline + SessionTimelineItem.ToolResultActivity(
                key = markerKey,
                toolKey = toolKey,
            ),
            timelineMutation = RealtimeTimelineMutation.Unknown,
        )
    }

    private fun RealtimeSessionProjection.appendToolOutput(
        payload: JsonObject,
    ): RealtimeSessionProjection {
        val toolId = payload.projectionToolId() ?: return this
        val activityKey = "tool:$toolId"
        val sequence = payload.firstLong("sequence", "seq")
        if (sequence != null && seenToolOutputSequences[toolId]?.contains(sequence) == true) {
            return this
        }
        val delta = payload.firstString(
            "text",
            "delta",
            "output",
            "output_text",
            "content",
        ).orEmpty()
        val existingActivityIndex = timeline.indexOfTimelineKey(activityKey)
        val existingActivity = timeline.getOrNull(existingActivityIndex)
            as? SessionTimelineItem.ToolActivity
        val seeded = if (existingActivity == null) {
            upsertTool(payload, ToolActivityStatus.RUNNING)
        } else {
            this
        }
        val previousIndex = seeded.timeline.indexOfTimelineKey(activityKey)
        val previous = seeded.timeline.getOrNull(previousIndex)
            as? SessionTimelineItem.ToolActivity
            ?: return seeded.copy(timelineMutation = RealtimeTimelineMutation.Unknown)
        val output = if (existingActivity == null && previous.output.isNotEmpty()) {
            previous.output
        } else {
            appendBoundedRealtimeToolOutput(previous.output, delta)
        }
        val existingProjection = seeded.tools.firstOrNull { it.key == toolId }
        val mergedPayload = JsonObject(
            buildMap {
                previous.payload.filterKeys { key -> key !in TOOL_OUTPUT_TRANSPORT_KEYS }.let(::putAll)
                existingProjection?.payload
                    ?.filterKeys { key -> key !in TOOL_OUTPUT_TRANSPORT_KEYS }
                    ?.let(::putAll)
                payload.filterKeys { key -> key !in TOOL_OUTPUT_TRANSPORT_KEYS }.let(::putAll)
                put("output", JsonPrimitive(output))
            },
        )
        val activity = previous.copy(output = output, payload = mergedPayload)
        val updatedSequences = if (sequence == null) {
            seeded.seenToolOutputSequences
        } else {
            seeded.seenToolOutputSequences +
                (toolId to boundedOutputSequences(
                    seeded.seenToolOutputSequences[toolId].orEmpty(),
                    sequence,
                ))
        }
        return seeded.replaceTimelineItemAt(previousIndex, activity).copy(
            tools = seeded.tools.map { tool ->
                if (tool.key == toolId) tool.copy(payload = mergedPayload) else tool
            },
            seenToolOutputSequences = updatedSequences,
        )
    }

    private fun boundedRealtimeToolOutput(value: String): String {
        if (value.codePointCount(0, value.length) <= MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS) {
            return value
        }
        val markerCodePoints = REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER.codePointCount(
            0,
            REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER.length,
        )
        val retainedCodePoints = MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS - markerCodePoints
        val retainedEnd = value.offsetByCodePoints(0, retainedCodePoints)
        return value.substring(0, retainedEnd) + REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER
    }

    private fun appendBoundedRealtimeToolOutput(
        current: String,
        delta: String,
    ): String {
        if (current.endsWith(REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER)) return current
        val currentCodePoints = current.codePointCount(0, current.length)
        val deltaCodePoints = delta.codePointCount(0, delta.length)
        if (currentCodePoints + deltaCodePoints <= MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS) {
            return current + delta
        }
        val markerCodePoints = REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER.codePointCount(
            0,
            REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER.length,
        )
        val maxContentCodePoints = MAX_REALTIME_TOOL_OUTPUT_CODE_POINTS - markerCodePoints
        if (currentCodePoints > maxContentCodePoints) {
            val retainedCurrentEnd = current.offsetByCodePoints(0, maxContentCodePoints)
            return current.substring(0, retainedCurrentEnd) + REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER
        }
        val retainedDeltaCodePoints = maxContentCodePoints - currentCodePoints
        val retainedDeltaEnd = delta.offsetByCodePoints(0, retainedDeltaCodePoints)
        return current + delta.substring(0, retainedDeltaEnd) + REALTIME_TOOL_OUTPUT_TRUNCATED_MARKER
    }

    private fun boundedOutputSequences(
        existing: Set<Long>,
        sequence: Long,
    ): Set<Long> {
        val updated = LinkedHashSet<Long>(minOf(existing.size + 1, MAX_TRACKED_TOOL_OUTPUT_SEQUENCES))
        updated.addAll(existing)
        updated.add(sequence)
        while (updated.size > MAX_TRACKED_TOOL_OUTPUT_SEQUENCES) {
            val iterator = updated.iterator()
            iterator.next()
            iterator.remove()
        }
        return updated
    }

    private fun RealtimeSessionProjection.appendTerminalOutput(
        payload: JsonObject,
    ): RealtimeSessionProjection {
        val processId = payload.firstString("process_id") ?: return this
        val turnId = payload.firstString("turn_id")
        val projectionId = turnId?.let {
            V2LifecycleProjectionKey.encode(
                "terminal",
                V2LifecycleIdentity(it, processId),
            )
        } ?: "process:$processId"
        val mapped = JsonObject(
            buildMap {
                putAll(payload)
                put("tool_id", JsonPrimitive(projectionId))
                put("name", JsonPrimitive("terminal"))
                put("text", JsonPrimitive(payload.firstString("text", "chunk").orEmpty()))
            },
        )
        return appendToolOutput(mapped)
    }

    private fun RealtimeSessionProjection.completeTerminalOutput(
        payload: JsonObject,
    ): RealtimeSessionProjection {
        val processId = payload.firstString("process_id") ?: return this
        val turnId = payload.firstString("turn_id")
        val projectionId = turnId?.let { "$it:terminal:$processId" } ?: "process:$processId"
        val exitCode = payload.firstLong("exit_code")
        val completionReason = payload.firstString("completion_reason")
        val status = when {
            completionReason == "killed" -> ToolActivityStatus.INTERRUPTED
            exitCode == 0L -> ToolActivityStatus.COMPLETE
            exitCode != null -> ToolActivityStatus.ERROR
            else -> ToolActivityStatus.UNKNOWN
        }
        val mapped = JsonObject(
            buildMap {
                putAll(payload)
                put("tool_id", JsonPrimitive(projectionId))
                put("name", JsonPrimitive("terminal"))
                if (status == ToolActivityStatus.ERROR && payload.firstString("error") == null) {
                    val message = if (exitCode == null) {
                        "Process completion status is unknown."
                    } else {
                        "Process exited with code $exitCode."
                    }
                    put("error", JsonPrimitive(message))
                }
            },
        )
        return upsertTool(mapped, status).appendToolResultMarker(mapped)
    }

    private fun RealtimeSessionProjection.upsertSubagent(
        payload: JsonObject,
        status: LiveSubagentStatus,
    ): RealtimeSessionProjection {
        val turnKey = activeAssistantTurnKey ?: return this
        val identity = payload.firstString("subagent_id", "child_session_id")
            ?: payload.firstLong("task_index")?.toString()
            ?: return this
        val key = "$turnKey:subagent:$identity"
        val existing = subagents.firstOrNull { it.key == key }
        val parentKey = payload.firstString("parent_id")
            ?.let { parentIdentity ->
                if (parentIdentity.startsWith("$turnKey:subagent:")) {
                    parentIdentity
                } else {
                    "$turnKey:subagent:$parentIdentity"
                }
            }
            ?: existing?.parentKey
        val replacement = LiveSubagentProjection(
            key = key,
            turnKey = turnKey,
            parentKey = parentKey,
            goal = HermesMessagePresentation.safeText(payload.firstString("goal"))
                ?: existing?.goal.orEmpty(),
            model = HermesMessagePresentation.safeText(
                payload.firstString("model"),
                maxCodePoints = 160,
            )
                ?: existing?.model,
            status = status,
            summary = HermesMessagePresentation.safeText(payload.firstString("summary"))
                ?: existing?.summary,
            durationSeconds = payload.firstDouble("duration_seconds")
                ?: existing?.durationSeconds,
            taskIndex = payload.firstLong("task_index") ?: existing?.taskIndex,
            taskCount = payload.firstLong("task_count") ?: existing?.taskCount,
            inputTokens = payload.firstLong("input_tokens") ?: existing?.inputTokens,
            outputTokens = payload.firstLong("output_tokens") ?: existing?.outputTokens,
            reasoningTokens = payload.firstLong("reasoning_tokens")
                ?: existing?.reasoningTokens,
            apiCalls = payload.firstLong("api_calls") ?: existing?.apiCalls,
        )
        return copy(
            subagents = if (existing == null) {
                subagents + replacement
            } else {
                subagents.map { if (it.key == key) replacement else it }
            },
        ).ensureProcessMarker(turnKey)
    }

    private fun RealtimeSessionProjection.upsertMoaReference(
        payload: JsonObject,
    ): RealtimeSessionProjection {
        val turnKey = activeAssistantTurnKey ?: return this
        val label = payload.firstString("label") ?: return this
        val index = payload.firstLong("index")?.toInt()
        val identity = index?.toString() ?: label
        val key = "$turnKey:moa:$identity"
        val existing = moaReferences.firstOrNull { it.key == key }
        val replacement = LiveMoaReferenceProjection(
            key = key,
            turnKey = turnKey,
            label = HermesMessagePresentation.safeText(label, maxCodePoints = 160).orEmpty(),
            text = HermesMessagePresentation.safeText(payload.firstString("text"))
                ?: existing?.text.orEmpty(),
            index = index ?: existing?.index,
            count = payload.firstLong("count")?.toInt() ?: existing?.count,
        )
        return copy(
            moaReferences = if (existing == null) {
                moaReferences + replacement
            } else {
                moaReferences.map { if (it.key == key) replacement else it }
            },
        ).ensureProcessMarker(turnKey)
    }

    private fun RealtimeSessionProjection.upsertMoaProgress(
        eventType: String,
        payload: JsonObject,
    ): RealtimeSessionProjection {
        val turnKey = activeAssistantTurnKey ?: return this
        val existing = moaProgress.firstOrNull { it.turnKey == turnKey }
        val replacement = LiveMoaProgressProjection(
            turnKey = turnKey,
            phase = HermesMessagePresentation.safeText(
                payload.firstString("phase"),
                maxCodePoints = 160,
            )
                ?: if (eventType == "moa.aggregating") "aggregating" else existing?.phase,
            aggregator = HermesMessagePresentation.safeText(
                payload.firstString("aggregator"),
                maxCodePoints = 160,
            )
                ?: existing?.aggregator,
            refsDone = payload.firstLong("refs_done")?.toInt() ?: existing?.refsDone,
            refsTotal = payload.firstLong("refs_total")?.toInt() ?: existing?.refsTotal,
        )
        return copy(
            moaProgress = if (existing == null) {
                moaProgress + replacement
            } else {
                moaProgress.map { if (it.turnKey == turnKey) replacement else it }
            },
        ).ensureProcessMarker(turnKey)
    }

    private fun RealtimeSessionProjection.ensureProcessMarker(
        turnKey: String,
    ): RealtimeSessionProjection {
        val key = "$turnKey:process"
        if (timeline.any { it.key == key }) return this
        return copy(
            timeline = timeline + SessionTimelineItem.ProcessActivity(
                key = key,
                turnKey = turnKey,
            ),
            timelineMutation = RealtimeTimelineMutation.Unknown,
        )
    }

    private fun RealtimeSessionProjection.appendAssistantText(
        delta: String,
        cursor: EventCursor,
    ): RealtimeSessionProjection = updateAssistantSegment(cursor, AssistantSegmentKind.RESPONSE) {
        it.copy(text = it.text + delta)
    }

    private fun RealtimeSessionProjection.updateAssistantSegment(
        cursor: EventCursor,
        kind: AssistantSegmentKind,
        transform: (SessionTimelineItem.AssistantTurn) -> SessionTimelineItem.AssistantTurn,
    ): RealtimeSessionProjection {
        val ensured = ensureAssistantTurn(cursor)
        val turnKey = ensured.activeAssistantTurnKey ?: return ensured
        val last = ensured.timeline.lastOrNull()
        val reusable = last is SessionTimelineItem.AssistantTurn &&
            last.turnKey == turnKey &&
            (last.segmentKind == kind || (last.segmentKind == null && last.isEmptySegment()))
        val target = if (reusable) {
            last
        } else {
            SessionTimelineItem.AssistantTurn(
                key = "$turnKey:segment:${cursor.ordinal}",
                turnKey = turnKey,
                segmentKind = kind,
            )
        }
        val replacement = transform(target.copy(segmentKind = kind))
        return if (reusable) {
            ensured.replaceTimelineItemAt(ensured.timeline.lastIndex, replacement)
        } else {
            ensured.copy(
                timeline = ensured.timeline + replacement,
                timelineMutation = RealtimeTimelineMutation.Unknown,
            )
        }
    }

    private fun RealtimeSessionProjection.replaceAssistantText(
        text: String,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val turnKey = activeAssistantTurnKey ?: return this
        if (text.isEmpty()) return this
        val target = timeline.lastOrNull() as? SessionTimelineItem.AssistantTurn
        if (target?.turnKey != turnKey || target.segmentKind != AssistantSegmentKind.RESPONSE) {
            return updateAssistantSegment(cursor, AssistantSegmentKind.RESPONSE) {
                it.copy(text = text)
            }
        }
        val stablePrefix = assistantSegments(turnKey)
            .asSequence()
            .filter { it.segmentKind == AssistantSegmentKind.RESPONSE && it.key != target.key }
            .joinToString(separator = "") { it.text }
        val segmentText = if (stablePrefix.isNotEmpty() && text.startsWith(stablePrefix)) {
            text.removePrefix(stablePrefix)
        } else {
            text
        }
        val current = target.text
        return when {
            segmentText == current || current.startsWith(segmentText) -> this
            segmentText.startsWith(current) -> replaceTimelineItemAt(
                timeline.lastIndex,
                target.copy(text = segmentText),
            )
            else -> replaceTimelineItemAt(
                timeline.lastIndex,
                target.copy(text = segmentText),
            )
        }
    }

    private fun RealtimeSessionProjection.replaceAssistantReasoning(
        text: String,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val turnKey = activeAssistantTurnKey ?: return this
        if (text.isEmpty()) return this
        val target = timeline.lastOrNull() as? SessionTimelineItem.AssistantTurn
        if (target?.turnKey != turnKey || target.segmentKind != AssistantSegmentKind.REASONING) {
            return updateAssistantSegment(cursor, AssistantSegmentKind.REASONING) {
                it.copy(reasoning = text)
            }
        }
        val current = target.reasoning
        return when {
            text == current || current.startsWith(text) -> this
            else -> replaceTimelineItemAt(
                timeline.lastIndex,
                target.copy(reasoning = text),
            )
        }
    }

    private fun RealtimeSessionProjection.sealAssistantTurn(
        text: String,
        status: AssistantTurnStatus,
        error: String?,
        cursor: EventCursor,
    ): RealtimeSessionProjection {
        val activeKey = activeAssistantTurnKey ?: return this
        val reconciled = replaceAssistantText(text, cursor)
        val terminalIndex = reconciled.timeline.indexOfLast { item ->
            item is SessionTimelineItem.AssistantTurn && item.turnKey == activeKey
        }
        val terminal = reconciled.timeline.getOrNull(terminalIndex)
            as? SessionTimelineItem.AssistantTurn
            ?: return reconciled
        return reconciled.replaceTimelineItemAt(
            terminalIndex,
            terminal.copy(status = status, error = error),
        ).copy(
            activeAssistantTurnKey = null,
        )
    }

    private fun RealtimeSessionProjection.replaceTimelineItemAt(
        index: Int,
        replacement: SessionTimelineItem,
    ): RealtimeSessionProjection {
        val previous = timeline.getOrNull(index)
            ?: return copy(timelineMutation = RealtimeTimelineMutation.Unknown)
        if (previous.key != replacement.key) {
            return copy(timelineMutation = RealtimeTimelineMutation.Unknown)
        }
        val updated = TimelineItemOverlayList.replacing(timeline, index, replacement)
        val mutation = timelineMutation
        val nextMutation = if (
            mutation.sourceTimeline != null &&
            (mutation.firstChangedIndex == null || mutation.firstChangedIndex == index)
        ) {
            RealtimeTimelineMutation(
                sourceTimeline = mutation.sourceTimeline,
                firstChangedIndex = index,
            )
        } else {
            RealtimeTimelineMutation.Unknown
        }
        return copy(timeline = updated, timelineMutation = nextMutation)
    }

    private fun RealtimeSessionProjection.assistantSegments(
        turnKey: String,
    ): List<SessionTimelineItem.AssistantTurn> = timeline
        .filterIsInstance<SessionTimelineItem.AssistantTurn>()
        .filter { it.turnKey == turnKey }

    private fun SessionTimelineItem.AssistantTurn.isEmptySegment(): Boolean =
        text.isEmpty() && reasoning.isEmpty() && thinking.isEmpty() && statusText.isEmpty()

    private fun mergeStreamedText(current: String, incoming: String): String = when {
        incoming.isEmpty() -> current
        current.isEmpty() -> incoming
        incoming == current -> current
        incoming.startsWith(current) -> incoming
        current.startsWith(incoming) -> current
        else -> current + incoming
    }

    private fun JsonElement.hasProtocolContent(): Boolean = when (this) {
        JsonNull -> false
        is JsonPrimitive -> content.isNotBlank()
        is JsonObject -> values.any { value -> value.hasProtocolContent() }
        is JsonArray -> any { value -> value.hasProtocolContent() }
    }

    private fun RealtimeSessionProjection.updatePrompt(
        clientPromptId: String,
        transform: (OutboundPrompt) -> OutboundPrompt,
    ): RealtimeSessionProjection {
        require(outboundPrompts.any { it.clientPromptId == clientPromptId }) {
            "Client prompt id is not present."
        }
        return copy(
            outboundPrompts = outboundPrompts.map {
                if (it.clientPromptId == clientPromptId) transform(it) else it
            },
        )
    }

    private fun JsonObject.string(key: String): String =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content.orEmpty()

    private fun JsonObject.stringOrNull(key: String): String? =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun <T> preservesExistingMetadata(
        existing: T?,
        incoming: T?,
        incomingPresent: Boolean,
    ): Boolean = existing == null || !incomingPresent || existing == incoming

    private fun preservesExistingJsonMetadata(
        existing: JsonObject,
        incoming: JsonObject,
        keys: Set<String>,
    ): Boolean = keys.all { key ->
        val previous = existing[key]
        previous == null ||
            !previous.hasProtocolContent() ||
            key !in incoming ||
            incoming[key] == previous
    }

    private fun JsonObject.firstString(vararg keys: String): String? =
        keys.firstNotNullOfOrNull { key -> string(key).takeIf(String::isNotBlank) }

    private fun JsonObject.projectionToolId(): String? {
        firstString("tool_id")?.let { return it }
        val identity = firstString("tool_call_id", "id", "call_id") ?: return null
        val turnId = firstString("turn_id")
        return turnId?.let {
            V2LifecycleProjectionKey.encode(
                "tool",
                V2LifecycleIdentity(it, identity),
            )
        } ?: identity
    }

    private fun JsonObject.firstLong(vararg keys: String): Long? =
        keys.firstNotNullOfOrNull { key -> (get(key) as? JsonPrimitive)?.longOrNull }

    private fun JsonObject.firstDouble(vararg keys: String): Double? =
        keys.firstNotNullOfOrNull { key -> (get(key) as? JsonPrimitive)?.doubleOrNull }

    private fun JsonObject.subagentCompletionStatus(): LiveSubagentStatus =
        when (firstString("status")?.lowercase()) {
            "failed", "error", "timeout" -> LiveSubagentStatus.ERROR
            "interrupted", "cancelled", "canceled" -> LiveSubagentStatus.INTERRUPTED
            else -> LiveSubagentStatus.COMPLETE
        }

    private fun validLifecycleRevision(
        existingRevision: Long?,
        existingFirstEventSequence: Long?,
        incomingRevision: Long,
        incomingFirstEventSequence: Long,
        cursor: EventCursor,
    ): Boolean {
        if (incomingFirstEventSequence <= 0 || incomingFirstEventSequence > cursor.ordinal) return false
        return if (existingRevision == null) {
            incomingRevision == 1L
        } else {
            existingFirstEventSequence == incomingFirstEventSequence &&
                incomingRevision == existingRevision + 1
        }
    }

    private fun String?.toTodoStatus(): HermesConversationTodoStatus = when (this) {
        "in_progress" -> HermesConversationTodoStatus.IN_PROGRESS
        "completed" -> HermesConversationTodoStatus.COMPLETED
        "cancelled" -> HermesConversationTodoStatus.CANCELLED
        else -> HermesConversationTodoStatus.PENDING
    }

    private fun String?.toSubagentStatus(): LiveSubagentStatus = when (this) {
        "completed" -> LiveSubagentStatus.COMPLETE
        "failed" -> LiveSubagentStatus.ERROR
        "interrupted" -> LiveSubagentStatus.INTERRUPTED
        else -> LiveSubagentStatus.RUNNING
    }

    private fun String?.toToolStatus(): LiveToolStatus = when (this) {
        "completed" -> LiveToolStatus.COMPLETE
        "failed" -> LiveToolStatus.ERROR
        "interrupted" -> LiveToolStatus.INTERRUPTED
        "unknown" -> LiveToolStatus.UNKNOWN
        else -> LiveToolStatus.RUNNING
    }

    private fun LiveToolStatus.toToolActivityStatus(): ToolActivityStatus = when (this) {
        LiveToolStatus.RUNNING -> ToolActivityStatus.RUNNING
        LiveToolStatus.COMPLETE -> ToolActivityStatus.COMPLETE
        LiveToolStatus.ERROR -> ToolActivityStatus.ERROR
        LiveToolStatus.INTERRUPTED -> ToolActivityStatus.INTERRUPTED
        LiveToolStatus.UNKNOWN -> ToolActivityStatus.UNKNOWN
    }

    private fun List<LiveSubagentProjection>.isValidV2SubagentGraph(): Boolean {
        if (size > MAX_V2_SUBAGENTS) return false
        if (any { it.identity == null }) return false
        val byIdentity = associateBy { requireNotNull(it.identity) }
        return all { agent ->
            var current = agent
            var depth = 1
            val seen = hashSetOf(requireNotNull(agent.identity))
            while (current.parentIdentity != null) {
                val parent = byIdentity[current.parentIdentity] ?: return@all false
                if (!seen.add(requireNotNull(parent.identity))) return@all false
                depth += 1
                if (depth > MAX_V2_SUBAGENT_DEPTH) return@all false
                current = parent
            }
            true
        }
    }

    private fun <T> List<T>.upsertSorted(
        replacement: T,
        key: (T) -> String,
        order: T.() -> Pair<Long, String>,
    ): List<T> {
        val replacementKey = key(replacement)
        return (filterNot { key(it) == replacementKey } + replacement)
            .sortedWith(compareBy<T> { it.order().first }.thenBy { it.order().second })
    }

    private fun JsonObject.boolean(key: String): Boolean? =
        (get(key) as? JsonPrimitive)?.booleanOrNull
}

private val V2_LIFECYCLE_EVENT_TYPES = setOf(
    "todo.update",
    "subagent.update",
    "tool.update",
    "terminal.update",
)
private val TERMINAL_TODO_STATUSES = setOf(
    HermesConversationTodoStatus.COMPLETED,
    HermesConversationTodoStatus.CANCELLED,
)
private val TERMINAL_SUBAGENT_STATUSES = setOf(
    LiveSubagentStatus.COMPLETE,
    LiveSubagentStatus.ERROR,
    LiveSubagentStatus.INTERRUPTED,
)
private val TERMINAL_TOOL_STATUSES = setOf(
    LiveToolStatus.COMPLETE,
    LiveToolStatus.ERROR,
    LiveToolStatus.INTERRUPTED,
)
private val TOOL_ABSORBING_METADATA_FIELDS = setOf(
    "name",
    "call_label",
    "summary",
    "duration_ms",
)
private const val MAX_V2_SUBAGENTS = 128
private const val MAX_V2_SUBAGENT_DEPTH = 8
