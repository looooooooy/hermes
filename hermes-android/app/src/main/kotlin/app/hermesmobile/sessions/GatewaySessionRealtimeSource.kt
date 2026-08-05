package app.hermesmobile.sessions

import app.hermesmobile.protocol.GatewayEndpoint
import app.hermesmobile.protocol.auth.ClientInstanceId
import app.hermesmobile.protocol.gateway.GatewayConnection
import app.hermesmobile.protocol.gateway.GatewayEvent
import app.hermesmobile.protocol.gateway.CloudObserverEventContract
import app.hermesmobile.protocol.gateway.GatewaySocketObserver
import app.hermesmobile.protocol.gateway.GatewaySocketState
import app.hermesmobile.protocol.gateway.GatewayWebSocketClient
import app.hermesmobile.protocol.gateway.SessionObserverClient
import app.hermesmobile.protocol.gateway.SessionObserverResult
import app.hermesmobile.protocol.gateway.SessionObserverSnapshotMessage
import app.hermesmobile.protocol.gateway.SessionObserverSubscription
import app.hermesmobile.protocol.gateway.SessionObserverSubscriptionId
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.channelFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.longOrNull
import kotlin.math.max

internal fun mergeObserverSnapshotMessages(
    baseline: SessionTranscript,
    snapshot: List<SessionObserverSnapshotMessage>,
): SessionTranscript {
    val baselineOffset = baseline.pagination.offset
    if (baselineOffset > snapshot.size) {
        return baseline
    }
    val snapshotMessages = snapshot.map { message ->
        SessionMessageProjection(
            messageId = null,
            role = message.role,
            content = message.content?.let(::JsonPrimitive),
            timestampEpochSeconds = null,
            reasoning = null,
            reasoningContent = null,
            reasoningDetails = null,
            toolCallId = null,
            toolCalls = null,
            toolName = null,
            displayKind = null,
            displayMetadata = null,
        )
    }
    val mergedSize = max(
        snapshotMessages.size,
        baselineOffset + baseline.messages.size,
    )
    val merged = MutableList<SessionMessageProjection?>(mergedSize) { null }
    snapshotMessages.forEachIndexed { index, message ->
        merged[index] = message
    }
    baseline.messages.forEachIndexed { index, message ->
        merged[baselineOffset + index] = message
    }
    return baseline.copy(
        messages = merged.filterNotNull(),
        pagination = baseline.pagination.copy(
            offset = 0,
            returned = merged.count { it != null },
        ),
    )
}

/**
 * Ticketed, read-only observation of one authoritative live Hermes session.
 *
 * Every reconnect installs a fresh REST baseline before accepting a new event
 * epoch. The observer RPC never resumes, activates, or otherwise rebinds the
 * session owner transport.
 */
class GatewaySessionRealtimeSource(
    private val endpoint: GatewayEndpoint,
    private val ticketProvider: WebSocketTicketSource,
    private val clientInstanceId: ClientInstanceId,
    private val socketClient: GatewayWebSocketClient,
    private val transcriptSource: SessionTranscriptSource,
    private val reducer: RealtimeSessionReducer = RealtimeSessionReducer(),
    private val reconnectDelayMillis: Long = DEFAULT_RECONNECT_DELAY_MILLIS,
    private val readyTimeoutMillis: Long = DEFAULT_READY_TIMEOUT_MILLIS,
    private val observerContractVersion: Int = 2,
) : SessionRealtimeSource {
    private val resyncCoordinator = RealtimeResyncCoordinator(transcriptSource, reducer)

    override fun observe(
        session: SessionProjection,
        baseline: SessionTranscript,
    ): Flow<SessionRealtimeUpdate> = channelFlow {
        var projection: RealtimeSessionProjection? = null
        var currentBaseline = baseline
        var epoch = 1L
        var reconnecting = false

        while (currentCoroutineContext().isActive) {
            send(
                SessionRealtimeUpdate.Connection(
                    status = if (reconnecting) {
                        RealtimeConnectionStatus.RECONNECTING
                    } else {
                        RealtimeConnectionStatus.CONNECTING
                    },
                    controlStatus = RealtimeControlStatus.OBSERVER,
                ),
            )

            if (reconnecting) {
                val current = projection
                if (current == null) {
                    when (
                        val resync = transcriptSource.loadMessages(
                            sessionKey = session.sessionKey,
                            profile = session.profile,
                        )
                    ) {
                        is SessionRepositoryResult.Data -> currentBaseline = resync.value
                        SessionRepositoryResult.AuthenticationRequired -> {
                            send(authenticationRequiredUpdate())
                            return@channelFlow
                        }
                        is SessionRepositoryResult.Unavailable -> {
                            send(disconnectedUpdate(resync.summary))
                            delayBeforeReconnect()
                            continue
                        }
                    }
                } else {
                    epoch = current.connectionEpoch + 1
                    when (
                        val resync = resyncCoordinator.resync(
                            current = current,
                            connectionEpoch = epoch,
                            profile = session.profile,
                        )
                    ) {
                        is RealtimeResyncResult.Ready -> {
                            projection = resync.projection
                            currentBaseline = resync.projection.transcript
                            send(SessionRealtimeUpdate.Projection(resync.projection))
                        }
                        RealtimeResyncResult.AuthenticationRequired -> {
                            send(authenticationRequiredUpdate())
                            return@channelFlow
                        }
                        is RealtimeResyncResult.Unavailable -> {
                            send(disconnectedUpdate(resync.summary))
                            delayBeforeReconnect()
                            continue
                        }
                    }
                }
            }

            val ticket = when (val ticket = ticketProvider.mint(clientInstanceId)) {
                is WebSocketTicketResult.Ready -> ticket.ticket
                WebSocketTicketResult.AuthenticationRequired -> {
                    send(authenticationRequiredUpdate())
                    return@channelFlow
                }
                is WebSocketTicketResult.Unavailable -> {
                    send(disconnectedUpdate(ticket.summary))
                    reconnecting = true
                    delayBeforeReconnect()
                    continue
                }
            }

            val signals = Channel<SocketSignal>(Channel.UNLIMITED)
            val connection = socketClient.connect(
                endpoint = endpoint,
                ticket = ticket,
                observerContractVersion = observerContractVersion,
                observer = object : GatewaySocketObserver {
                    override fun onStateChanged(state: GatewaySocketState) {
                        signals.trySend(SocketSignal.State(state))
                    }

                    override fun onEvent(event: GatewayEvent) {
                        signals.trySend(SocketSignal.Event(event))
                    }

                    override fun onProtocolError() {
                        signals.trySend(SocketSignal.ProtocolError)
                    }
                },
            )
            val observerClient = SessionObserverClient(connection)
            var subscriptionId: SessionObserverSubscriptionId? = null
            var shouldReconnect = false
            try {
                val ready = withTimeoutOrNull(readyTimeoutMillis) {
                    awaitReady(signals)
                } == true
                if (!ready) {
                    shouldReconnect = true
                } else {
                    if (connection.capabilities?.supportsSessionObserver(observerContractVersion) != true) {
                        send(
                            SessionRealtimeUpdate.Connection(
                                status = RealtimeConnectionStatus.UNSUPPORTED,
                                controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
                                message = "Hermes server does not support read-only session observers.",
                            ),
                        )
                        return@channelFlow
                    }

                    when (
                        val subscribe = observerClient.subscribe(
                            sessionKey = session.sessionKey,
                            profile = session.profile,
                            observerContractVersion = observerContractVersion,
                        )
                    ) {
                    is SessionObserverResult.Success -> {
                        val subscription = subscribe.value
                        subscriptionId = subscription.subscriptionId
                        currentBaseline = mergeObserverSnapshotMessages(
                            currentBaseline,
                            subscription.messages,
                        )
                        projection = installSubscriptionSnapshot(
                            current = projection,
                            baseline = currentBaseline,
                            subscription = subscription,
                            connectionEpoch = epoch,
                        )
                        subscription.replayEvents.forEach { replayEvent ->
                            val beforeReplay = requireNotNull(projection)
                            val afterReplay = reducer.apply(
                                current = requireNotNull(projection),
                                event = replayEvent,
                                cursor = EventCursor(
                                    connectionEpoch = epoch,
                                    ordinal = requireNotNull(replayEvent.eventSequence),
                                ),
                            )
                            if (
                                afterReplay === beforeReplay &&
                                replayEvent.type in V2_LIFECYCLE_EVENT_TYPES
                            ) {
                                send(protocolErrorUpdate())
                                return@channelFlow
                            }
                            projection = afterReplay.rememberObserverTransportDigest(replayEvent)
                        }
                        send(SessionRealtimeUpdate.Projection(requireNotNull(projection)))
                        send(
                            SessionRealtimeUpdate.Connection(
                                status = RealtimeConnectionStatus.LIVE,
                                controlStatus = RealtimeControlStatus.OBSERVER,
                            ),
                        )
                        shouldReconnect = consumeEvents(
                            signals = signals,
                            session = session,
                            connectionEpoch = epoch,
                            initial = requireNotNull(projection),
                        ) { updated ->
                            projection = updated
                            send(SessionRealtimeUpdate.Projection(updated))
                        }
                    }
                    SessionObserverResult.Unsupported -> {
                        send(
                            SessionRealtimeUpdate.Connection(
                                status = RealtimeConnectionStatus.UNSUPPORTED,
                                controlStatus = RealtimeControlStatus.SERVER_UPGRADE_REQUIRED,
                                message = "Hermes server does not support read-only session observers.",
                            ),
                        )
                        return@channelFlow
                    }
                    is SessionObserverResult.RpcFailure -> {
                        when (subscribe.error.code) {
                            OBSERVER_REPLAY_UNAVAILABLE_CODE -> shouldReconnect = true
                            LIVE_SESSION_NOT_FOUND_CODE -> {
                                send(disconnectedUpdate("This session is not currently running."))
                                return@channelFlow
                            }
                            else -> {
                                send(
                                    SessionRealtimeUpdate.Connection(
                                        status = RealtimeConnectionStatus.ERROR,
                                        controlStatus = RealtimeControlStatus.OBSERVER,
                                        message = HermesMessagePresentation.safeText(
                                            subscribe.error.message,
                                            maxCodePoints = MAX_REALTIME_DIAGNOSTIC_CODE_POINTS,
                                        ) ?: "Realtime observer request failed.",
                                    ),
                                )
                                return@channelFlow
                            }
                        }
                    }
                    SessionObserverResult.InvalidResponse -> {
                        send(protocolErrorUpdate())
                        return@channelFlow
                    }
                        SessionObserverResult.NotReady,
                        SessionObserverResult.Disconnected,
                        SessionObserverResult.Timeout,
                        -> shouldReconnect = true
                    }
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } finally {
                val id = subscriptionId
                if (id != null && connection.state == GatewaySocketState.Ready) {
                    withContext(NonCancellable) {
                        withTimeoutOrNull(UNSUBSCRIBE_TIMEOUT_MILLIS) {
                            observerClient.unsubscribe(id, observerContractVersion)
                        }
                    }
                }
                connection.close()
                signals.close()
            }

            if (!shouldReconnect || !currentCoroutineContext().isActive) return@channelFlow
            reconnecting = true
            send(disconnectedUpdate("Realtime connection was interrupted."))
            delayBeforeReconnect()
        }
    }

    private suspend fun consumeEvents(
        signals: Channel<SocketSignal>,
        session: SessionProjection,
        connectionEpoch: Long,
        initial: RealtimeSessionProjection,
        onProjection: suspend (RealtimeSessionProjection) -> Unit,
    ): Boolean {
        var current = initial
        for (signal in signals) {
            when (signal) {
                is SocketSignal.State -> when (signal.value) {
                    is GatewaySocketState.Closed,
                    is GatewaySocketState.Failed,
                    -> return true
                    else -> Unit
                }
                is SocketSignal.Event -> {
                    val event = signal.value
                    if (event.type == GATEWAY_READY_EVENT) continue
                    if (event.sessionKey != session.sessionKey) continue
                    if (observerContractVersion == 2) {
                        if (
                            event.observerContractVersion != 2 ||
                            event.profile != current.observerProfile ||
                            event.runtimeGeneration != current.runtimeGeneration ||
                            event.runtimeSessionId != current.runtimeSessionId
                        ) {
                            return true
                        }
                    }
                    if (!CloudObserverEventPolicy.accepts(observerContractVersion, event)) return true
                    val sequence = event.eventSequence ?: return true
                    if (sequence <= current.lastEventOrdinal) {
                        val digest = event.transportDigest ?: return true
                        if (current.seenObserverTransportDigests[sequence] == digest) continue
                        return true
                    }
                    if (sequence != current.lastEventOrdinal + 1) return true
                    val updated = reducer.apply(
                        current = current,
                        event = event,
                        cursor = EventCursor(connectionEpoch, sequence),
                    )
                    if (updated === current && event.type in V2_LIFECYCLE_EVENT_TYPES) return true
                    if (updated != current) {
                        current = updated.rememberObserverTransportDigest(event)
                        onProjection(current)
                    }
                }
                SocketSignal.ProtocolError -> return true
            }
        }
        return true
    }

    private suspend fun awaitReady(signals: Channel<SocketSignal>): Boolean {
        for (signal in signals) {
            when (signal) {
                is SocketSignal.State -> when (signal.value) {
                    GatewaySocketState.Ready -> return true
                    is GatewaySocketState.Closed,
                    is GatewaySocketState.Failed,
                    -> return false
                    else -> Unit
                }
                SocketSignal.ProtocolError -> return false
                is SocketSignal.Event -> Unit
            }
        }
        return false
    }

    private fun installSubscriptionSnapshot(
        current: RealtimeSessionProjection?,
        baseline: SessionTranscript,
        subscription: SessionObserverSubscription,
        connectionEpoch: Long,
    ): RealtimeSessionProjection {
        val base = if (
            current == null ||
            current.runtimeSessionId != subscription.runtimeSessionId ||
            current.runtimeGeneration != subscription.runtimeGeneration
        ) {
            reducer.seed(baseline, subscription.runtimeSessionId, connectionEpoch)
        } else {
            current.copy(
                transcript = baseline,
                lineageTip = baseline.lineageTip,
                connectionEpoch = connectionEpoch,
            )
        }
        val assistantText = subscription.inflight?.assistant.orEmpty()
        val inflightError = subscription.inflight?.error
        val snapshotKey = if (subscription.running || inflightError != null) {
            "assistant:$connectionEpoch:snapshot"
        } else {
            null
        }
        val activeKey = snapshotKey.takeIf { subscription.running }
        val snapshotTimeline = if (snapshotKey == null) {
            emptyList()
        } else {
            listOf(
                SessionTimelineItem.AssistantTurn(
                    key = snapshotKey,
                    text = assistantText,
                    status = if (inflightError == null) {
                        AssistantTurnStatus.STREAMING
                    } else {
                        AssistantTurnStatus.ERROR
                    },
                    error = inflightError,
                ),
            )
        }
        val todoSections = subscription.todoSections.map { section ->
            val identity = V2LifecycleIdentity(section.turnId, section.sectionId)
            LiveTodoSectionProjection(
                key = V2LifecycleProjectionKey.encode("todo", identity),
                turnKey = section.turnId,
                revision = section.revision,
                firstEventSequence = section.firstEventSequence,
                status = section.status.toTodoStatus(),
                items = section.items.map { item ->
                    LiveTodoItemProjection(
                        key = item.safeString("id") ?: return@map LiveTodoItemProjection(
                            key = "invalid",
                            label = "Task",
                            status = HermesConversationTodoStatus.PENDING,
                        ),
                        label = HermesMessagePresentation.safeText(item.safeString("label")) ?: "Task",
                        status = item.safeString("status").toTodoStatus(),
                    )
                },
                identity = identity,
            )
        }
        val subagents = subscription.subagents.map { agent ->
            val payload = agent.payload
            val progress = payload["progress"] as? JsonObject
            val counts = payload["token_counts"] as? JsonObject
            val identity = V2LifecycleIdentity(agent.turnId, agent.subagentId)
            val parentIdentity = agent.parentSubagentId?.let {
                V2LifecycleIdentity(agent.turnId, it)
            }
            LiveSubagentProjection(
                key = V2LifecycleProjectionKey.encode("subagent", identity),
                turnKey = agent.turnId,
                parentKey = parentIdentity?.let {
                    V2LifecycleProjectionKey.encode("subagent", it)
                },
                goal = HermesMessagePresentation.safeText(payload.safeString("goal")).orEmpty(),
                model = HermesMessagePresentation.safeText(payload.safeString("model"), 160),
                status = payload.safeString("status").toSubagentStatus(),
                summary = HermesMessagePresentation.safeText(payload.safeString("summary")),
                durationSeconds = payload.safeLong("duration_ms")?.div(1_000.0),
                taskIndex = progress?.safeLong("current"),
                taskCount = progress?.safeLong("total"),
                inputTokens = counts?.safeLong("input"),
                outputTokens = counts?.safeLong("output"),
                reasoningTokens = counts?.safeLong("reasoning"),
                apiCalls = payload.safeLong("api_calls"),
                name = HermesMessagePresentation.safeText(payload.safeString("name"), 160) ?: "Subagent",
                revision = agent.revision,
                firstEventSequence = agent.firstEventSequence,
                identity = identity,
                parentIdentity = parentIdentity,
            )
        }
        val tools = subscription.tools.map { tool ->
            val identity = V2LifecycleIdentity(tool.turnId, tool.toolCallId)
            LiveToolProjection(
                key = V2LifecycleProjectionKey.encode("tool", identity),
                name = HermesMessagePresentation.safeText(tool.payload.safeString("name"), 160),
                status = tool.payload.safeString("status").toToolStatus(),
                payload = tool.payload,
                turnKey = tool.turnId,
                revision = tool.revision,
                firstEventSequence = tool.firstEventSequence,
                identity = identity,
            )
        }
        val terminals = subscription.terminals.map { terminal ->
            val identity = V2LifecycleIdentity(terminal.turnId, terminal.processId)
            LiveTerminalProjection(
                key = V2LifecycleProjectionKey.encode("terminal", identity),
                turnKey = terminal.turnId,
                revision = terminal.revision,
                firstEventSequence = terminal.firstEventSequence,
                status = terminal.payload.safeString("status").toToolStatus(),
                exitCode = terminal.payload.safeLong("exit_code")?.toInt(),
                summary = HermesMessagePresentation.safeText(terminal.payload.safeString("summary")),
                durationSeconds = terminal.payload.safeLong("duration_ms")?.div(1_000.0),
                identity = identity,
            )
        }
        val processTurns = (todoSections.map(LiveTodoSectionProjection::turnKey) +
            subagents.map(LiveSubagentProjection::turnKey)).distinct()
        val processTimeline = processTurns.map { turnKey ->
            SessionTimelineItem.ProcessActivity(
                key = "$turnKey:process",
                turnKey = turnKey,
            )
        }
        val toolTimeline = subscription.tools.map { tool ->
            val key = V2LifecycleProjectionKey.encode(
                "tool",
                V2LifecycleIdentity(tool.turnId, tool.toolCallId),
            )
            SessionTimelineItem.ToolActivity(
                key = "tool:$key",
                toolId = key,
                name = HermesMessagePresentation.safeText(tool.payload.safeString("name"), 160),
                summary = HermesMessagePresentation.safeText(tool.payload.safeString("summary")),
                durationSeconds = tool.payload.safeLong("duration_ms")?.div(1_000.0),
                status = tool.payload.safeString("status").toToolStatus().toToolActivityStatus(),
                payload = tool.payload,
            )
        }
        val terminalTimeline = subscription.terminals.map { terminal ->
            val key = V2LifecycleProjectionKey.encode(
                "terminal",
                V2LifecycleIdentity(terminal.turnId, terminal.processId),
            )
            SessionTimelineItem.ToolActivity(
                key = "tool:$key",
                toolId = key,
                name = "terminal",
                summary = HermesMessagePresentation.safeText(terminal.payload.safeString("summary")),
                durationSeconds = terminal.payload.safeLong("duration_ms")?.div(1_000.0),
                status = terminal.payload.safeString("status").toToolStatus().toToolActivityStatus(),
                payload = terminal.payload,
            )
        }
        return base.copy(
            runtimeSessionId = subscription.runtimeSessionId,
            lastEventOrdinal = subscription.snapshotEventSequence,
            running = subscription.running,
            streamingAssistantText = assistantText,
            streamingReasoningText = "",
            liveMessages = emptyList(),
            todoSections = todoSections,
            tools = tools,
            subagents = subagents,
            terminals = terminals,
            timeline = snapshotTimeline + processTimeline + toolTimeline + terminalTimeline,
            activeAssistantTurnKey = activeKey,
            pendingInput = null,
            lastError = inflightError,
            activeToolIds = (tools.filter { it.status == LiveToolStatus.RUNNING }.map { it.key } +
                terminals.filter { it.status == LiveToolStatus.RUNNING }.map { it.key }).toSet(),
            seenToolOutputSequences = emptyMap(),
            observerContractVersion = subscription.observerContractVersion,
            observerProfile = subscription.profile,
            runtimeGeneration = subscription.runtimeGeneration,
            seenObserverTransportDigests = emptyMap(),
        )
    }

    private suspend fun kotlinx.coroutines.channels.ProducerScope<SessionRealtimeUpdate>.delayBeforeReconnect() {
        if (reconnectDelayMillis > 0) delay(reconnectDelayMillis)
    }

    private fun disconnectedUpdate(message: String) = SessionRealtimeUpdate.Connection(
        status = RealtimeConnectionStatus.DISCONNECTED,
        controlStatus = RealtimeControlStatus.OBSERVER,
        message = message,
    )

    private fun authenticationRequiredUpdate() = SessionRealtimeUpdate.Connection(
        status = RealtimeConnectionStatus.ERROR,
        controlStatus = RealtimeControlStatus.OBSERVER,
        message = "Sign in again to observe this session.",
    )

    private fun protocolErrorUpdate() = SessionRealtimeUpdate.Connection(
        status = RealtimeConnectionStatus.ERROR,
        controlStatus = RealtimeControlStatus.OBSERVER,
        message = "Hermes returned an invalid realtime observer response.",
    )

    private sealed interface SocketSignal {
        data class State(val value: GatewaySocketState) : SocketSignal
        data class Event(val value: GatewayEvent) : SocketSignal
        data object ProtocolError : SocketSignal
    }

    private companion object {
        const val GATEWAY_READY_EVENT = "gateway.ready"
        const val LIVE_SESSION_NOT_FOUND_CODE = 4001
        const val OBSERVER_REPLAY_UNAVAILABLE_CODE = 4091
        const val DEFAULT_RECONNECT_DELAY_MILLIS = 1_000L
        const val DEFAULT_READY_TIMEOUT_MILLIS = 10_000L
        const val UNSUBSCRIBE_TIMEOUT_MILLIS = 1_500L
        const val MAX_REALTIME_DIAGNOSTIC_CODE_POINTS = 512
        const val MAX_OBSERVER_TRANSPORT_DIGESTS = 1_024
    }

    private fun RealtimeSessionProjection.rememberObserverTransportDigest(
        event: GatewayEvent,
    ): RealtimeSessionProjection {
        if (observerContractVersion != 2) return this
        val sequence = event.eventSequence ?: return this
        val digest = event.transportDigest ?: return this
        val updated = LinkedHashMap(seenObserverTransportDigests)
        updated[sequence] = digest
        while (updated.size > MAX_OBSERVER_TRANSPORT_DIGESTS) {
            updated.remove(updated.keys.first())
        }
        return copy(seenObserverTransportDigests = updated)
    }
}

internal object CloudObserverEventPolicy {
    fun accepts(type: String): Boolean = type in CloudObserverEventContract.eventTypes

    fun accepts(event: GatewayEvent): Boolean =
        CloudObserverEventContract.accepts(event.type, event.payload)

    fun accepts(observerContractVersion: Int, event: GatewayEvent): Boolean =
        event.type in CloudObserverEventContract.eventTypes(observerContractVersion) &&
            CloudObserverEventContract.accepts(observerContractVersion, event.type, event.payload)
}

private val V2_LIFECYCLE_EVENT_TYPES = setOf(
    "todo.update",
    "subagent.update",
    "tool.update",
    "terminal.update",
)

private fun JsonObject.safeString(key: String): String? =
    (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

private fun JsonObject.safeLong(key: String): Long? =
    (get(key) as? JsonPrimitive)?.takeUnless { it.isString }?.longOrNull

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
