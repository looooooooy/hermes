package app.hermesmobile.protocol.gateway

import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put

@JvmInline
value class SessionObserverSubscriptionId(val value: String) {
    init {
        require(value.isNotBlank()) { "Observer subscription id is required." }
    }
}

@JvmInline
value class SessionObserverStatus(val value: String) {
    init {
        require(value.isNotBlank()) { "Observer status is required." }
    }
}

data class SessionObserverSnapshotMessage(
    val role: String,
    val content: String? = null,
)

data class SessionObserverInflight(
    val user: String? = null,
    val assistant: String? = null,
    val streaming: Boolean = false,
    val error: String? = null,
)

data class SessionObserverTodoSection(
    val turnId: String,
    val sectionId: String,
    val revision: Long,
    val firstEventSequence: Long,
    val status: String,
    val items: List<JsonObject>,
)

data class SessionObserverSubagent(
    val turnId: String,
    val subagentId: String,
    val revision: Long,
    val firstEventSequence: Long,
    val parentSubagentId: String?,
    val payload: JsonObject,
)

data class SessionObserverTool(
    val turnId: String,
    val toolCallId: String,
    val revision: Long,
    val firstEventSequence: Long,
    val payload: JsonObject,
)

data class SessionObserverTerminal(
    val turnId: String,
    val processId: String,
    val revision: Long,
    val firstEventSequence: Long,
    val payload: JsonObject,
)

data class SessionObserverSubscription(
    val subscriptionId: SessionObserverSubscriptionId,
    val sessionKey: SessionKey,
    val runtimeSessionId: RuntimeSessionId,
    val running: Boolean,
    val status: SessionObserverStatus,
    val snapshotEventSequence: Long,
    val eventSequence: Long,
    val messages: List<SessionObserverSnapshotMessage>,
    val inflight: SessionObserverInflight?,
    val replayEvents: List<GatewayEvent>,
    val observerContractVersion: Int = 1,
    val profile: String? = null,
    val runtimeGeneration: String? = null,
    val todoSections: List<SessionObserverTodoSection> = emptyList(),
    val subagents: List<SessionObserverSubagent> = emptyList(),
    val tools: List<SessionObserverTool> = emptyList(),
    val terminals: List<SessionObserverTerminal> = emptyList(),
)

sealed interface SessionObserverResult<out T> {
    data class Success<T>(val value: T) : SessionObserverResult<T>
    data object Unsupported : SessionObserverResult<Nothing>
    data object NotReady : SessionObserverResult<Nothing>
    data object Disconnected : SessionObserverResult<Nothing>
    data object Timeout : SessionObserverResult<Nothing>
    data class RpcFailure(val error: JsonRpcError) : SessionObserverResult<Nothing>
    data object InvalidResponse : SessionObserverResult<Nothing>
}

class SessionObserverClient(
    private val connection: GatewayConnection,
) {
    suspend fun subscribe(
        sessionKey: SessionKey,
        profile: String? = null,
        observerContractVersion: Int = 2,
    ): SessionObserverResult<SessionObserverSubscription> {
        if (connection.capabilities?.supportsSessionObserver(observerContractVersion) != true) {
            return SessionObserverResult.Unsupported
        }
        if (observerContractVersion == 2 && !profile.isValidProfile()) {
            return SessionObserverResult.InvalidResponse
        }
        val params = buildJsonObject {
            if (observerContractVersion == 2) put("observer_contract", 2)
            put("session_key", sessionKey.value)
            profile?.takeIf(String::isNotBlank)?.let { put("profile", it) }
        }
        return when (val result = connection.call(SUBSCRIBE_METHOD, params)) {
            is GatewayCallResult.Success -> when (observerContractVersion) {
                1 -> decodeV1Subscription(result.value, sessionKey)
                2 -> decodeV2Subscription(result.value, sessionKey, requireNotNull(profile))
                else -> SessionObserverResult.Unsupported
            }
            is GatewayCallResult.RpcFailure -> result.error.toObserverFailure()
            GatewayCallResult.NotReady -> SessionObserverResult.NotReady
            GatewayCallResult.Disconnected -> SessionObserverResult.Disconnected
            GatewayCallResult.Timeout -> SessionObserverResult.Timeout
        }
    }

    suspend fun unsubscribe(
        subscriptionId: SessionObserverSubscriptionId,
        observerContractVersion: Int = 2,
    ): SessionObserverResult<Unit> {
        if (connection.capabilities?.supportsSessionObserver(observerContractVersion) != true) {
            return SessionObserverResult.Unsupported
        }
        val params = buildJsonObject {
            if (observerContractVersion == 2) put("observer_contract", 2)
            put("subscription_id", subscriptionId.value)
        }
        return when (val result = connection.call(UNSUBSCRIBE_METHOD, params)) {
            is GatewayCallResult.Success -> {
                val value = result.value as? JsonObject
                val valid = if (observerContractVersion == 2) {
                    value?.keys == setOf("observer_contract") &&
                        (value["observer_contract"] as? JsonPrimitive)?.longOrNull == 2L
                } else {
                    value?.isEmpty() == true
                }
                if (valid) {
                    SessionObserverResult.Success(Unit)
                } else {
                    SessionObserverResult.InvalidResponse
                }
            }
            is GatewayCallResult.RpcFailure -> result.error.toObserverFailure()
            GatewayCallResult.NotReady -> SessionObserverResult.NotReady
            GatewayCallResult.Disconnected -> SessionObserverResult.Disconnected
            GatewayCallResult.Timeout -> SessionObserverResult.Timeout
        }
    }

    private fun decodeV2Subscription(
        element: JsonElement,
        expectedSessionKey: SessionKey,
        expectedProfile: String,
    ): SessionObserverResult<SessionObserverSubscription> {
        val value = element as? JsonObject ?: return SessionObserverResult.InvalidResponse
        if (value.keys != V2_SUBSCRIPTION_FIELDS) return SessionObserverResult.InvalidResponse
        if ((value["observer_contract"] as? JsonPrimitive)?.longOrNull != 2L) {
            return SessionObserverResult.InvalidResponse
        }
        if (!CloudObserverEventContract.acceptsDisplaySafeV2(value)) {
            return SessionObserverResult.InvalidResponse
        }
        val profile = value.requiredString("profile")
            ?.takeIf { it == expectedProfile && it.isValidProfile() }
            ?: return SessionObserverResult.InvalidResponse
        val generation = value.requiredString("runtime_generation")
            ?.takeIf { it.length <= 128 }
            ?: return SessionObserverResult.InvalidResponse
        val subscriptionId = value.requiredString("subscription_id")
            ?.takeIf { it.length <= 256 }
            ?.let(::SessionObserverSubscriptionId)
            ?: return SessionObserverResult.InvalidResponse
        val sessionKey = value.requiredString("session_key")
            ?.takeIf { it.length <= 256 }
            ?.let(::SessionKey)
            ?: return SessionObserverResult.InvalidResponse
        if (sessionKey != expectedSessionKey) return SessionObserverResult.InvalidResponse
        val runtimeSessionId = value.requiredString("runtime_session_id")
            ?.takeIf { it.length <= 256 }
            ?.let(::RuntimeSessionId)
            ?: return SessionObserverResult.InvalidResponse
        val running = (value["running"] as? JsonPrimitive)?.booleanOrNull
            ?: return SessionObserverResult.InvalidResponse
        val status = value.requiredString("status")
            ?.takeIf { it.length <= 64 }
            ?.let(::SessionObserverStatus)
            ?: return SessionObserverResult.InvalidResponse
        if (running != (status.value in RUNNING_STATUSES)) {
            return SessionObserverResult.InvalidResponse
        }
        val eventSequence = value.nonNegativeLong("event_sequence")
            ?: return SessionObserverResult.InvalidResponse
        val snapshotEventSequence = value.nonNegativeLong("snapshot_event_sequence")
            ?.takeIf { it <= eventSequence }
            ?: return SessionObserverResult.InvalidResponse
        val messages = (value["messages"] as? JsonArray)
            ?.takeIf { it.size <= 500 }
            ?.map {
                decodeMessage(it)?.takeIf { message ->
                    message.role.hasAtMostObserverV2CodePoints(64) &&
                        message.content?.hasAtMostObserverV2CodePoints(
                            OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS,
                        ) != false
                } ?: return SessionObserverResult.InvalidResponse
            }
            ?: return SessionObserverResult.InvalidResponse
        val inflight = decodeInflight(value["inflight"])
            ?: return SessionObserverResult.InvalidResponse

        val todoSections = decodeV2TodoSnapshot(value["todo_sections"], snapshotEventSequence)
            ?: return SessionObserverResult.InvalidResponse
        val subagents = decodeV2SubagentSnapshot(value["subagents"], snapshotEventSequence)
            ?: return SessionObserverResult.InvalidResponse
        val tools = decodeV2ToolSnapshot(value["tools"], snapshotEventSequence)
            ?: return SessionObserverResult.InvalidResponse
        val terminals = decodeV2TerminalSnapshot(value["terminals"], snapshotEventSequence)
            ?: return SessionObserverResult.InvalidResponse
        if (!subagents.hasValidSubagentGraph()) return SessionObserverResult.InvalidResponse

        val replayEvents = when (val rawEvents = value["replay_events"]) {
            is JsonArray -> {
                var previousSequence = snapshotEventSequence
                rawEvents.map { raw ->
                    val event = (raw as? JsonObject)
                        ?.let(CloudObserverEventContract::decodeV2SessionEvent)
                        ?: return SessionObserverResult.InvalidResponse
                    if (
                        event.sessionKey != sessionKey ||
                        event.runtimeSessionId != runtimeSessionId ||
                        event.profile != profile ||
                        event.runtimeGeneration != generation
                    ) {
                        return SessionObserverResult.InvalidResponse
                    }
                    val sequence = event.eventSequence ?: return SessionObserverResult.InvalidResponse
                    if (event.eventSequenceStart != previousSequence + 1 || sequence > eventSequence) {
                        return SessionObserverResult.InvalidResponse
                    }
                    previousSequence = sequence
                    event
                }.also {
                    if (it.size > OBSERVER_V2_MAX_ARRAY_ITEMS || previousSequence != eventSequence) {
                        return SessionObserverResult.InvalidResponse
                    }
                }
            }
            else -> return SessionObserverResult.InvalidResponse
        }
        if (replayEvents.isEmpty() && snapshotEventSequence != eventSequence) {
            return SessionObserverResult.InvalidResponse
        }
        if (!validateV2ReplayProjection(todoSections, subagents, tools, terminals, replayEvents)) {
            return SessionObserverResult.InvalidResponse
        }
        return SessionObserverResult.Success(
            SessionObserverSubscription(
                subscriptionId = subscriptionId,
                sessionKey = sessionKey,
                runtimeSessionId = runtimeSessionId,
                running = running,
                status = status,
                snapshotEventSequence = snapshotEventSequence,
                eventSequence = eventSequence,
                messages = messages,
                inflight = inflight,
                replayEvents = replayEvents,
                observerContractVersion = 2,
                profile = profile,
                runtimeGeneration = generation,
                todoSections = todoSections,
                subagents = subagents,
                tools = tools,
                terminals = terminals,
            ),
        )
    }

    private fun decodeV2TodoSnapshot(
        element: JsonElement?,
        cursor: Long,
    ): List<SessionObserverTodoSection>? {
        val array = (element as? JsonArray)?.takeIf { it.size <= 64 } ?: return null
        val identities = HashSet<Pair<String, String>>()
        return array.map { raw ->
            val value = raw as? JsonObject ?: return null
            val payload = JsonObject(value + ("operation" to JsonPrimitive("upsert")))
            if (!CloudObserverEventContract.accepts(2, "todo.update", payload)) return null
            val turnId = value.requiredString("turn_id") ?: return null
            val sectionId = value.requiredString("section_id") ?: return null
            if (!identities.add(turnId to sectionId)) return null
            val first = value.positiveLong("first_event_sequence")?.takeIf { it <= cursor } ?: return null
            SessionObserverTodoSection(
                turnId = turnId,
                sectionId = sectionId,
                revision = value.positiveLong("revision") ?: return null,
                firstEventSequence = first,
                status = value.requiredString("status") ?: return null,
                items = (value["items"] as JsonArray).map { it as JsonObject },
            )
        }.sortedWith(compareBy(SessionObserverTodoSection::firstEventSequence, SessionObserverTodoSection::sectionId))
    }

    private fun decodeV2SubagentSnapshot(
        element: JsonElement?,
        cursor: Long,
    ): List<SessionObserverSubagent>? {
        val array = (element as? JsonArray)?.takeIf { it.size <= 128 } ?: return null
        val identities = HashSet<Pair<String, String>>()
        return array.map { raw ->
            val value = raw as? JsonObject ?: return null
            val payload = JsonObject(value + ("operation" to JsonPrimitive("upsert")))
            if (!CloudObserverEventContract.accepts(2, "subagent.update", payload)) return null
            val turnId = value.requiredString("turn_id") ?: return null
            val subagentId = value.requiredString("subagent_id") ?: return null
            if (!identities.add(turnId to subagentId)) return null
            val first = value.positiveLong("first_event_sequence")?.takeIf { it <= cursor } ?: return null
            val parent = value.requiredNullableString("parent_subagent_id") ?: return null
            SessionObserverSubagent(
                turnId = turnId,
                subagentId = subagentId,
                revision = value.positiveLong("revision") ?: return null,
                firstEventSequence = first,
                parentSubagentId = parent.value,
                payload = value,
            )
        }.sortedWith(compareBy(SessionObserverSubagent::firstEventSequence, SessionObserverSubagent::subagentId))
    }

    private fun decodeV2ToolSnapshot(
        element: JsonElement?,
        cursor: Long,
    ): List<SessionObserverTool>? {
        val array = (element as? JsonArray)?.takeIf { it.size <= 128 } ?: return null
        val identities = HashSet<Pair<String, String>>()
        return array.map { raw ->
            val value = raw as? JsonObject ?: return null
            val payload = JsonObject(value + ("operation" to JsonPrimitive("upsert")))
            if (!CloudObserverEventContract.accepts(2, "tool.update", payload)) return null
            val turnId = value.requiredString("turn_id") ?: return null
            val toolId = value.requiredString("tool_call_id") ?: return null
            if (!identities.add(turnId to toolId)) return null
            val first = value.positiveLong("first_event_sequence")?.takeIf { it <= cursor } ?: return null
            SessionObserverTool(
                turnId = turnId,
                toolCallId = toolId,
                revision = value.positiveLong("revision") ?: return null,
                firstEventSequence = first,
                payload = value,
            )
        }.sortedWith(compareBy(SessionObserverTool::firstEventSequence, SessionObserverTool::toolCallId))
    }

    private fun decodeV2TerminalSnapshot(
        element: JsonElement?,
        cursor: Long,
    ): List<SessionObserverTerminal>? {
        val array = (element as? JsonArray)?.takeIf { it.size <= 128 } ?: return null
        val identities = HashSet<Pair<String, String>>()
        return array.map { raw ->
            val value = raw as? JsonObject ?: return null
            val payload = JsonObject(value + ("operation" to JsonPrimitive("upsert")))
            if (!CloudObserverEventContract.accepts(2, "terminal.update", payload)) return null
            val turnId = value.requiredString("turn_id") ?: return null
            val processId = value.requiredString("process_id") ?: return null
            if (!identities.add(turnId to processId)) return null
            val first = value.positiveLong("first_event_sequence")?.takeIf { it <= cursor } ?: return null
            SessionObserverTerminal(
                turnId = turnId,
                processId = processId,
                revision = value.positiveLong("revision") ?: return null,
                firstEventSequence = first,
                payload = value,
            )
        }.sortedWith(compareBy(SessionObserverTerminal::firstEventSequence, SessionObserverTerminal::processId))
    }

    private fun List<SessionObserverSubagent>.hasValidSubagentGraph(): Boolean {
        val byTurn = groupBy(SessionObserverSubagent::turnId)
        return byTurn.values.all { turnAgents ->
            if (turnAgents.size > 128) return@all false
            val byId = turnAgents.associateBy(SessionObserverSubagent::subagentId)
            turnAgents.all { agent ->
                var depth = 1
                var current = agent
                val seen = hashSetOf(agent.subagentId)
                while (current.parentSubagentId != null) {
                    val parent = byId[current.parentSubagentId] ?: return@all false
                    if (!seen.add(parent.subagentId)) return@all false
                    depth += 1
                    if (depth > 8) return@all false
                    current = parent
                }
                true
            }
        }
    }

    private fun validateV2ReplayProjection(
        todoSections: List<SessionObserverTodoSection>,
        subagents: List<SessionObserverSubagent>,
        tools: List<SessionObserverTool>,
        terminals: List<SessionObserverTerminal>,
        replayEvents: List<GatewayEvent>,
    ): Boolean {
        val entities = LinkedHashMap<ReplayIdentity, ReplayEntity>()
        todoSections.forEach { section ->
            entities[ReplayIdentity("todo", section.turnId, section.sectionId)] = ReplayEntity(
                revision = section.revision,
                firstEventSequence = section.firstEventSequence,
                status = section.status,
                todoItems = section.items.map { item ->
                    ReplayTodoItem(
                        id = item.requiredString("id").orEmpty(),
                        label = item.requiredString("label").orEmpty(),
                        status = item.requiredString("status").orEmpty(),
                    )
                },
            )
        }
        subagents.forEach { agent ->
            entities[ReplayIdentity("subagent", agent.turnId, agent.subagentId)] = ReplayEntity(
                revision = agent.revision,
                firstEventSequence = agent.firstEventSequence,
                status = agent.payload.requiredString("status").orEmpty(),
                parentIdentity = agent.parentSubagentId?.let {
                    ReplayIdentity("subagent", agent.turnId, it)
                },
                metadata = agent.payload,
            )
        }
        tools.forEach { tool ->
            entities[ReplayIdentity("tool", tool.turnId, tool.toolCallId)] = ReplayEntity(
                revision = tool.revision,
                firstEventSequence = tool.firstEventSequence,
                status = tool.payload.requiredString("status").orEmpty(),
                metadata = tool.payload,
            )
        }
        terminals.forEach { terminal ->
            entities[ReplayIdentity("terminal", terminal.turnId, terminal.processId)] = ReplayEntity(
                revision = terminal.revision,
                firstEventSequence = terminal.firstEventSequence,
                status = terminal.payload.requiredString("status").orEmpty(),
                metadata = terminal.payload,
            )
        }
        replayEvents.filter { it.type in V2_LIFECYCLE_TYPES }.forEach { event ->
            val payload = event.payload ?: return false
            val kind = event.type.substringBefore('.')
            val turnId = payload.requiredString("turn_id") ?: return false
            val entityId = payload.requiredString(V2_ENTITY_ID_FIELDS[kind] ?: return false)
                ?: return false
            val identity = ReplayIdentity(kind, turnId, entityId)
            val existing = entities[identity]
            val revision = payload.positiveLong("revision") ?: return false
            val first = payload.positiveLong("first_event_sequence") ?: return false
            if (existing == null) {
                if (revision != 1L || payload.requiredString("operation") != "upsert") return false
            } else if (
                revision != existing.revision + 1 ||
                first != existing.firstEventSequence
            ) {
                return false
            }
            if (payload.requiredString("operation") == "delete") {
                if (existing == null) return false
                if (
                    kind == "todo" &&
                    existing.todoItems.any { it.status !in TERMINAL_TODO_ITEM_STATUSES }
                ) {
                    return false
                }
                if (kind != "todo" && existing.status !in TERMINAL_STATUSES.getValue(kind)) return false
                if (
                    kind == "subagent" &&
                    entities.values.any { it.parentIdentity == identity }
                ) {
                    return false
                }
                entities.remove(identity)
                return@forEach
            }
            val status = payload.requiredString("status") ?: return false
            if (
                kind != "todo" &&
                existing?.status in TERMINAL_STATUSES.getValue(kind) &&
                existing?.status != status
            ) {
                return false
            }
            val parentIdentity = if (kind == "subagent") {
                val parent = payload.requiredNullableString("parent_subagent_id") ?: return false
                parent.value?.let { ReplayIdentity(kind, turnId, it) }?.also {
                    if (it !in entities) return false
                }
            } else {
                null
            }
            if (
                existing != null &&
                existing.status in TERMINAL_STATUSES.getValue(kind) &&
                !existing.acceptsAbsorbingMetadata(kind, payload, parentIdentity)
            ) {
                return false
            }
            val todoItems = if (kind == "todo") {
                val incoming = (payload["items"] as? JsonArray)?.map { raw ->
                    val item = raw as? JsonObject ?: return false
                    ReplayTodoItem(
                        id = item.requiredString("id") ?: return false,
                        label = item.requiredString("label") ?: return false,
                        status = item.requiredString("status") ?: return false,
                    )
                } ?: return false
                val previous = existing?.todoItems.orEmpty()
                if (
                    incoming.size < previous.size ||
                    previous.indices.any { index ->
                        previous[index].id != incoming[index].id ||
                            previous[index].label != incoming[index].label ||
                            (
                                previous[index].status in TERMINAL_TODO_ITEM_STATUSES &&
                                    previous[index].status != incoming[index].status
                            )
                    }
                ) {
                    return false
                }
                incoming
            } else {
                emptyList()
            }
            entities[identity] = ReplayEntity(
                revision = revision,
                firstEventSequence = first,
                status = status,
                parentIdentity = parentIdentity,
                todoItems = todoItems,
                metadata = JsonObject(existing?.metadata.orEmpty() + payload),
            )
            if (kind == "subagent" && !entities.hasValidReplaySubagentGraph()) return false
        }
        return true
    }

    private fun Map<ReplayIdentity, ReplayEntity>.hasValidReplaySubagentGraph(): Boolean {
        val subagents = filterKeys { it.kind == "subagent" }
        if (subagents.size > 128) return false
        return subagents.all { (identity, value) ->
            var currentIdentity = identity
            var current = value
            var depth = 1
            val seen = hashSetOf(identity)
            while (current.parentIdentity != null) {
                currentIdentity = current.parentIdentity
                if (!seen.add(currentIdentity)) return@all false
                current = subagents[currentIdentity] ?: return@all false
                depth += 1
                if (depth > 8) return@all false
            }
            true
        }
    }

    private data class ReplayEntity(
        val revision: Long,
        val firstEventSequence: Long,
        val status: String,
        val parentIdentity: ReplayIdentity? = null,
        val todoItems: List<ReplayTodoItem> = emptyList(),
        val metadata: JsonObject = JsonObject(emptyMap()),
    )

    private data class ReplayIdentity(
        val kind: String,
        val turnId: String,
        val entityId: String,
    )

    private data class ReplayTodoItem(
        val id: String,
        val label: String,
        val status: String,
    )

    private fun ReplayEntity.acceptsAbsorbingMetadata(
        kind: String,
        incoming: JsonObject,
        incomingParentIdentity: ReplayIdentity?,
    ): Boolean = when (kind) {
        "subagent" ->
            parentIdentity == incomingParentIdentity &&
                metadata.sameRequiredCoreValue(incoming, "name") &&
                metadata.onlyEnrichesValue(incoming, "goal") &&
                SUBAGENT_SAFE_METADATA_FIELDS.all { metadata.onlyEnrichesValue(incoming, it) }
        "tool" ->
            metadata.sameRequiredCoreValue(incoming, "name") &&
                TOOL_SAFE_METADATA_FIELDS.all { metadata.onlyEnrichesValue(incoming, it) }
        "terminal" -> TERMINAL_SAFE_METADATA_FIELDS.all {
            metadata.onlyEnrichesValue(incoming, it)
        }
        else -> true
    }

    private fun JsonObject.sameRequiredCoreValue(incoming: JsonObject, key: String): Boolean {
        val previous = get(key)
        return !previous.hasReplayMetadataContent() || incoming[key] == previous
    }

    private fun JsonObject.onlyEnrichesValue(incoming: JsonObject, key: String): Boolean {
        val previous = get(key)
        return !previous.hasReplayMetadataContent() || key !in incoming || incoming[key] == previous
    }

    private fun JsonElement?.hasReplayMetadataContent(): Boolean = when (this) {
        null, JsonNull -> false
        is JsonPrimitive -> !isString || content.isNotEmpty()
        else -> true
    }

    private fun decodeInflight(element: JsonElement?): SessionObserverInflight? {
        val value = element as? JsonObject ?: return null
        if (value.keys != INFLIGHT_FIELDS) return null
        val user = value.requiredNullableString("user") ?: return null
        val assistant = value.requiredNullableString("assistant") ?: return null
        val error = value.requiredNullableString("error") ?: return null
        if (
            user.value?.hasAtMostObserverV2CodePoints(
                OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS,
            ) == false ||
            assistant.value?.hasAtMostObserverV2CodePoints(
                OBSERVER_V2_MAX_STREAM_TEXT_CODE_POINTS,
            ) == false ||
            error.value?.hasAtMostObserverV2CodePoints(
                OBSERVER_V2_MAX_DISPLAY_TEXT_CODE_POINTS,
            ) == false
        ) {
            return null
        }
        val streaming = (value["streaming"] as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.booleanOrNull
            ?: return null
        return SessionObserverInflight(user.value, assistant.value, streaming, error.value)
    }

    private fun decodeV1Subscription(
        element: JsonElement,
        expectedSessionKey: SessionKey,
    ): SessionObserverResult<SessionObserverSubscription> {
        val value = element as? JsonObject ?: return SessionObserverResult.InvalidResponse
        if (value.keys != SUBSCRIPTION_FIELDS) {
            return SessionObserverResult.InvalidResponse
        }
        val subscriptionId = value.requiredString("subscription_id")
            ?.let(::SessionObserverSubscriptionId)
            ?: return SessionObserverResult.InvalidResponse
        val sessionKey = value.requiredString("session_key")
            ?.let(::SessionKey)
            ?: return SessionObserverResult.InvalidResponse
        if (sessionKey != expectedSessionKey) return SessionObserverResult.InvalidResponse
        val runtimeSessionId = value.requiredString("runtime_session_id")
            ?.let(::RuntimeSessionId)
            ?: return SessionObserverResult.InvalidResponse
        val running = (value["running"] as? JsonPrimitive)?.booleanOrNull
            ?: return SessionObserverResult.InvalidResponse
        val status = value.requiredString("status")
            ?.let(::SessionObserverStatus)
            ?: return SessionObserverResult.InvalidResponse
        if (running != (status.value in RUNNING_STATUSES)) {
            return SessionObserverResult.InvalidResponse
        }
        val eventSequence = (value["event_sequence"] as? JsonPrimitive)?.longOrNull
            ?.takeIf { it >= 0 }
            ?: return SessionObserverResult.InvalidResponse
        val snapshotEventSequence = when (val raw = value["snapshot_event_sequence"]) {
            is JsonPrimitive -> raw.longOrNull
                ?.takeIf { it >= 0 && it <= eventSequence }
                ?: return SessionObserverResult.InvalidResponse
            else -> return SessionObserverResult.InvalidResponse
        }
        val messages = when (val rawMessages = value["messages"]) {
            is JsonArray -> rawMessages.map { raw ->
                decodeMessage(raw) ?: return SessionObserverResult.InvalidResponse
            }
            else -> return SessionObserverResult.InvalidResponse
        }
        val inflight = when (val rawInflight = value["inflight"]) {
            is JsonObject -> {
                if (rawInflight.keys != INFLIGHT_FIELDS) {
                    return SessionObserverResult.InvalidResponse
                }
                val user = rawInflight.requiredNullableString("user")
                    ?: return SessionObserverResult.InvalidResponse
                val assistant = rawInflight.requiredNullableString("assistant")
                    ?: return SessionObserverResult.InvalidResponse
                val error = rawInflight.requiredNullableString("error")
                    ?: return SessionObserverResult.InvalidResponse
                val streaming = (rawInflight["streaming"] as? JsonPrimitive)
                    ?.takeUnless { it.isString }
                    ?.booleanOrNull
                    ?: return SessionObserverResult.InvalidResponse
                SessionObserverInflight(
                    user = user.value,
                    assistant = assistant.value,
                    streaming = streaming,
                    error = error.value,
                )
            }
            else -> return SessionObserverResult.InvalidResponse
        }
        val replayEvents = when (val rawEvents = value["replay_events"]) {
            is JsonArray -> {
                var previousSequence = snapshotEventSequence
                rawEvents.map { raw ->
                    val rawEvent = raw as? JsonObject
                        ?: return SessionObserverResult.InvalidResponse
                    val event = decodeReplayEvent(
                        element = rawEvent,
                        sessionKey = sessionKey,
                        runtimeSessionId = runtimeSessionId,
                    ) ?: return SessionObserverResult.InvalidResponse
                    val sequence = event.eventSequence
                        ?: return SessionObserverResult.InvalidResponse
                    val sequenceStart = when (val rawStart = rawEvent["event_sequence_start"]) {
                        null -> sequence
                        is JsonPrimitive -> rawStart.longOrNull
                            ?.takeIf { it > 0 && it <= sequence }
                            ?: return SessionObserverResult.InvalidResponse
                        else -> return SessionObserverResult.InvalidResponse
                    }
                    if (sequenceStart < sequence && event.type !in COALESCED_REPLAY_EVENT_TYPES) {
                        return SessionObserverResult.InvalidResponse
                    }
                    if (sequenceStart != previousSequence + 1 || sequence > eventSequence) {
                        return SessionObserverResult.InvalidResponse
                    }
                    previousSequence = sequence
                    event
                }.also {
                    if (it.isNotEmpty() && previousSequence != eventSequence) {
                        return SessionObserverResult.InvalidResponse
                    }
                }
            }
            else -> return SessionObserverResult.InvalidResponse
        }
        if (replayEvents.isEmpty() && snapshotEventSequence != eventSequence) {
            return SessionObserverResult.InvalidResponse
        }
        return SessionObserverResult.Success(
            SessionObserverSubscription(
                subscriptionId = subscriptionId,
                sessionKey = sessionKey,
                runtimeSessionId = runtimeSessionId,
                running = running,
                status = status,
                snapshotEventSequence = snapshotEventSequence,
                eventSequence = eventSequence,
                messages = messages,
                inflight = inflight,
                replayEvents = replayEvents,
            ),
        )
    }

    private fun decodeReplayEvent(
        element: JsonElement,
        sessionKey: SessionKey,
        runtimeSessionId: RuntimeSessionId,
    ): GatewayEvent? {
        val value = element as? JsonObject ?: return null
        if (!REPLAY_EVENT_FIELDS.containsAll(value.keys) || !value.keys.containsAll(REQUIRED_REPLAY_EVENT_FIELDS)) {
            return null
        }
        val type = value.requiredString("type") ?: return null
        if (type !in CloudObserverEventContract.eventTypes(1)) return null
        val eventRuntimeId = value.requiredString("session_id")
            ?.let(::RuntimeSessionId)
            ?: return null
        val eventSessionKey = value.requiredString("session_key")
            ?.let(::SessionKey)
            ?: return null
        val sequence = (value["event_sequence"] as? JsonPrimitive)?.longOrNull
            ?.takeIf { it > 0 }
            ?: return null
        val payload = when (val rawPayload = value["payload"]) {
            is JsonObject -> rawPayload
            else -> return null
        }
        if (!CloudObserverEventContract.accepts(1, type, payload)) return null
        if (eventRuntimeId != runtimeSessionId || eventSessionKey != sessionKey) return null
        return GatewayEvent(
            type = type,
            runtimeSessionId = eventRuntimeId,
            payload = payload,
            sessionKey = eventSessionKey,
            eventSequence = sequence,
        )
    }

    private fun decodeMessage(element: JsonElement): SessionObserverSnapshotMessage? {
        val value = runCatching { element.jsonObject }.getOrNull() ?: return null
        if ("role" !in value || !SNAPSHOT_MESSAGE_FIELDS.containsAll(value.keys)) return null
        val role = value.requiredString("role") ?: return null
        val content = when (val rawContent = value["content"]) {
            null,
            JsonNull,
            -> null

            is JsonPrimitive -> rawContent.takeIf { it.isString }?.content ?: return null
            else -> return null
        }
        return SessionObserverSnapshotMessage(
            role = role,
            content = content,
        )
    }

    private fun JsonObject.requiredString(key: String): String? =
        optionalString(key)?.takeIf(String::isNotBlank)

    private fun JsonObject.positiveLong(key: String): Long? =
        (get(key) as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.longOrNull
            ?.takeIf { it in 1..OBSERVER_V2_MAX_SAFE_INTEGER }

    private fun JsonObject.nonNegativeLong(key: String): Long? =
        (get(key) as? JsonPrimitive)
            ?.takeUnless { it.isString }
            ?.longOrNull
            ?.takeIf { it in 0..OBSERVER_V2_MAX_SAFE_INTEGER }

    private fun JsonObject.optionalString(key: String): String? =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun JsonObject.requiredNullableString(key: String): NullableStringField? =
        when (val value = get(key)) {
            JsonNull -> NullableStringField(null)
            is JsonPrimitive -> value
                .takeIf { it.isString }
                ?.content
                ?.let(::NullableStringField)
            else -> null
        }

    private data class NullableStringField(val value: String?)

    private fun String?.isValidProfile(): Boolean =
        this != null && length in 1..128 && PROFILE_PATTERN.matches(this)

    private fun JsonRpcError.toObserverFailure(): SessionObserverResult<Nothing> =
        if (
            code in OBSERVER_RPC_ERROR_CODES &&
            message.isNotBlank() &&
            message.length <= MAX_RPC_ERROR_MESSAGE_LENGTH &&
            (data == null || (data as? JsonObject)?.size?.let { it <= MAX_RPC_ERROR_DATA_FIELDS } == true)
        ) {
            SessionObserverResult.RpcFailure(this)
        } else {
            SessionObserverResult.InvalidResponse
        }

    private companion object {
        const val SUBSCRIBE_METHOD = "session.observe.subscribe"
        const val UNSUBSCRIBE_METHOD = "session.observe.unsubscribe"
        const val MAX_RPC_ERROR_MESSAGE_LENGTH = 4_096
        const val MAX_RPC_ERROR_DATA_FIELDS = 16
        val OBSERVER_RPC_ERROR_CODES = setOf(4_001, 4_091)
        val INFLIGHT_FIELDS = setOf("user", "assistant", "streaming", "error")
        val SNAPSHOT_MESSAGE_FIELDS = setOf("role", "content")
        val SUBSCRIPTION_FIELDS = setOf(
            "subscription_id",
            "session_key",
            "runtime_session_id",
            "running",
            "status",
            "event_sequence",
            "snapshot_event_sequence",
            "messages",
            "inflight",
            "replay_events",
        )
        val V2_SUBSCRIPTION_FIELDS = SUBSCRIPTION_FIELDS + setOf(
            "observer_contract",
            "profile",
            "runtime_generation",
            "todo_sections",
            "subagents",
            "tools",
            "terminals",
        )
        val PROFILE_PATTERN = Regex("^[A-Za-z0-9_.-]+$")
        val V2_LIFECYCLE_TYPES = setOf(
            "todo.update",
            "subagent.update",
            "tool.update",
            "terminal.update",
        )
        val V2_ENTITY_ID_FIELDS = mapOf(
            "todo" to "section_id",
            "subagent" to "subagent_id",
            "tool" to "tool_call_id",
            "terminal" to "process_id",
        )
        val TERMINAL_STATUSES = mapOf(
            "todo" to setOf("completed", "cancelled"),
            "subagent" to setOf("completed", "failed", "interrupted"),
            "tool" to setOf("completed", "failed", "interrupted"),
            "terminal" to setOf("completed", "failed", "interrupted"),
        )
        val TERMINAL_TODO_ITEM_STATUSES = setOf("completed", "cancelled")
        val SUBAGENT_SAFE_METADATA_FIELDS = setOf(
            "summary",
            "model",
            "duration_ms",
            "progress",
            "token_counts",
            "api_calls",
        )
        val TOOL_SAFE_METADATA_FIELDS = setOf(
            "call_label",
            "summary",
            "duration_ms",
        )
        val TERMINAL_SAFE_METADATA_FIELDS = setOf(
            "exit_code",
            "summary",
            "duration_ms",
        )
        val RUNNING_STATUSES = setOf("running", "working", "streaming")
        val COALESCED_REPLAY_EVENT_TYPES = setOf(
            "agent.terminal.output",
            "message.delta",
            "reasoning.delta",
            "status.update",
            "thinking.delta",
            "tool.output.delta",
        )
        val REPLAY_EVENT_FIELDS = setOf(
            "type",
            "session_id",
            "session_key",
            "event_sequence_start",
            "event_sequence",
            "payload",
        )
        val REQUIRED_REPLAY_EVENT_FIELDS = setOf(
            "type",
            "session_id",
            "session_key",
            "event_sequence",
            "payload",
        )
    }
}
