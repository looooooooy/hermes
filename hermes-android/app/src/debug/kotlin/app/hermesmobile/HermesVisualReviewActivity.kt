package app.hermesmobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import app.hermesmobile.protocol.gateway.SessionApprovalChoice
import app.hermesmobile.protocol.gateway.SessionClarifyChoice
import app.hermesmobile.protocol.gateway.SessionControlLease
import app.hermesmobile.protocol.gateway.SessionControlLeaseId
import app.hermesmobile.protocol.gateway.SessionControllerKind
import app.hermesmobile.protocol.gateway.SessionPendingInput
import app.hermesmobile.protocol.gateway.GatewayEvent
import app.hermesmobile.protocol.sessions.RuntimeSessionId
import app.hermesmobile.protocol.sessions.SessionKey
import app.hermesmobile.protocol.sessions.SessionMessageProjection
import app.hermesmobile.protocol.sessions.SessionProjection
import app.hermesmobile.protocol.sessions.SessionTranscript
import app.hermesmobile.protocol.sessions.TranscriptPagination
import app.hermesmobile.sessions.LiveToolProjection
import app.hermesmobile.sessions.LiveToolStatus
import app.hermesmobile.sessions.EventCursor
import app.hermesmobile.sessions.RealtimeConnectionStatus
import app.hermesmobile.sessions.RealtimeControlStatus
import app.hermesmobile.sessions.RealtimeSessionReducer
import app.hermesmobile.sessions.SessionBrowserPhase
import app.hermesmobile.sessions.SessionBrowserScreen
import app.hermesmobile.sessions.SessionBrowserUiState
import app.hermesmobile.sessions.control.ControlMode
import app.hermesmobile.sessions.control.ControlState
import app.hermesmobile.sessions.control.PendingInputInteractionOutcome
import app.hermesmobile.sessions.control.PendingInputInteractionState
import app.hermesmobile.ui.theme.HermesMobileTheme
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

private const val STREAMING_PERFORMANCE_MODE = "streaming-performance"
private const val STREAMING_PERFORMANCE_CYCLE_STEPS = 600L
private const val STREAMING_PERFORMANCE_STEP_DELAY_MS = 100L

/** Debug-only full-screen fixture for physical-device visual review. */
class HermesVisualReviewActivity : ComponentActivity() {
    private var reviewState by mutableStateOf(approvalReviewState())
    private val performanceReducer = RealtimeSessionReducer()
    private var performanceOrdinal = 0L
    private var performanceStep = 4L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val reviewMode = intent.getStringExtra("mode")
        val darkTheme = intent.getBooleanExtra("darkTheme", true)
        reviewState = when (reviewMode) {
            "clarify" -> clarifyReviewState()
            "recovery" -> recoveryReviewState()
            STREAMING_PERFORMANCE_MODE -> streamingPerformanceReviewState()
            else -> approvalReviewState()
        }
        performanceOrdinal = reviewState.realtime?.lastEventOrdinal ?: 0L
        enableEdgeToEdge()
        setContent {
            LaunchedEffect(reviewMode) {
                if (reviewMode == STREAMING_PERFORMANCE_MODE) {
                    runStreamingPerformanceFixture()
                }
            }
            HermesMobileTheme(darkTheme = darkTheme) {
                SessionBrowserScreen(
                    state = reviewState,
                    onOpenSession = {},
                    onBack = {},
                    onRefresh = {},
                    onLoadMore = {},
                    onReconnect = {},
                    onDraftChanged = {},
                    onSend = {},
                    onStop = {},
                    onPendingChoice = ::selectPendingChoice,
                    onPendingOtherChanged = ::updateOtherDraft,
                    onPendingSubmit = {},
                    onPendingConfirm = ::resolveApproval,
                    onPendingCancelConfirmation = ::cancelApprovalConfirmation,
                )
            }
        }
    }

    private suspend fun runStreamingPerformanceFixture() {
        while (true) {
            delay(STREAMING_PERFORMANCE_STEP_DELAY_MS)
            val realtime = reviewState.realtime ?: continue
            val cycle = performanceStep / STREAMING_PERFORMANCE_CYCLE_STEPS
            val stepInCycle = (performanceStep % STREAMING_PERFORMANCE_CYCLE_STEPS).toInt()
            val toolId = "performance-active-$cycle"
            val event = streamingPerformanceEvent(
                runtimeSessionId = realtime.runtimeSessionId,
                toolId = toolId,
                cycle = cycle,
                stepInCycle = stepInCycle,
            )
            performanceStep += 1
            performanceOrdinal += 1
            val updated = performanceReducer.apply(
                current = realtime,
                event = event,
                cursor = EventCursor(realtime.connectionEpoch, performanceOrdinal),
            )
            reviewState = reviewState.copy(realtime = updated)
        }
    }

    private fun selectPendingChoice(choiceId: String) {
        val pending = (reviewState.control.mode as? ControlMode.Controller)?.lease?.pendingInput
        reviewState = when (pending) {
            is SessionPendingInput.Approval -> {
                val choice = SessionApprovalChoice.fromWireValue(choiceId) ?: return
                reviewState.copy(
                    pendingInteraction = reviewState.pendingInteraction.copy(
                        selectedChoiceId = choiceId,
                        requiresConfirmation = choice == SessionApprovalChoice.ALLOW_ALWAYS,
                    ),
                )
            }
            is SessionPendingInput.Clarify -> reviewState.copy(
                pendingInteraction = reviewState.pendingInteraction.copy(
                    selectedChoiceId = choiceId,
                    otherDraft = "",
                ),
            )
            null -> reviewState
        }
    }

    private fun updateOtherDraft(text: String) {
        reviewState = reviewState.copy(
            pendingInteraction = reviewState.pendingInteraction.copy(
                selectedChoiceId = null,
                otherDraft = text,
            ),
        )
    }

    private fun resolveApproval() {
        reviewState = reviewState.copy(
            pendingInteraction = reviewState.pendingInteraction.copy(requiresConfirmation = false),
        )
    }

    private fun cancelApprovalConfirmation() {
        reviewState = reviewState.copy(
            pendingInteraction = reviewState.pendingInteraction.copy(
                selectedChoiceId = null,
                requiresConfirmation = false,
            ),
        )
    }
}

internal fun streamingPerformanceReviewState(): SessionBrowserUiState {
    val sessionKey = SessionKey("streaming-performance")
    val runtimeSessionId = RuntimeSessionId("runtime-streaming-performance")
    val history = buildList {
        repeat(96) { turn ->
            val userMessageId = turn * 2L + 1L
            add(
                SessionMessageProjection(
                    messageId = userMessageId,
                    role = "user",
                    content = JsonPrimitive(
                        "Performance prompt ${turn + 1}: inspect synthetic module-${turn % 12} and summarize the result.",
                    ),
                    timestampEpochSeconds = 1_000.0 + userMessageId,
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
                    messageId = userMessageId + 1L,
                    role = "assistant",
                    content = JsonPrimitive(
                        "## Completed response ${turn + 1}\n\n" +
                            "Synthetic history remains immutable while the active tail streams. " +
                            "Result marker: `${turn.toString().padStart(3, '0')}`.",
                    ),
                    timestampEpochSeconds = 1_001.0 + userMessageId,
                    reasoning = "Checked synthetic inputs for completed turn ${turn + 1}.",
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
    val transcript = SessionTranscript(
        sessionKey = sessionKey,
        lineageTip = sessionKey,
        messages = history,
        pagination = TranscriptPagination(
            limit = history.size,
            offset = 0,
            returned = history.size,
        ),
    )
    val session = SessionProjection(
        sessionKey = sessionKey,
        lineageRoot = sessionKey,
        lineageTip = sessionKey,
        parentSessionKey = null,
        title = "Streaming performance",
        preview = "Long synthetic transcript with reducer-backed live updates",
        source = "debug-fixture",
        model = "synthetic-performance-model",
        profile = null,
        cwd = null,
        gitBranch = null,
        startedAtEpochSeconds = 1_000.0,
        endedAtEpochSeconds = null,
        lastActiveEpochSeconds = 2_000.0,
        messageCount = history.size,
        toolCallCount = 5,
        inputTokens = 12_000,
        outputTokens = 18_000,
        isActive = true,
        archived = false,
    )
    val reducer = RealtimeSessionReducer()
    var realtime = reducer.seed(
        transcript = transcript,
        runtimeSessionId = runtimeSessionId,
        connectionEpoch = 1,
    )
    var ordinal = 0L

    fun apply(type: String, payload: JsonObject = JsonObject(emptyMap())) {
        ordinal += 1
        realtime = reducer.apply(
            current = realtime,
            event = GatewayEvent(
                type = type,
                runtimeSessionId = runtimeSessionId,
                payload = payload,
            ),
            cursor = EventCursor(connectionEpoch = 1, ordinal = ordinal),
        )
    }

    repeat(4) { turn ->
        val toolId = "performance-complete-$turn"
        apply("message.start")
        apply(
            "thinking.delta",
            buildJsonObject {
                put("text", "## Thinking\n\n- Reusing completed synthetic turn ${turn + 1}.\n")
            },
        )
        apply(
            "message.delta",
            buildJsonObject {
                put("text", "**Response ${turn + 1}:** completed through the realtime reducer.")
            },
        )
        apply(
            "tool.start",
            buildJsonObject {
                put("tool_id", toolId)
                put("name", "terminal")
                put("args_text", "printf 'synthetic completed turn ${turn + 1}'")
            },
        )
        apply(
            "tool.output.delta",
            buildJsonObject {
                put("tool_id", toolId)
                put("name", "terminal")
                put("sequence", 1L)
                put("text", "synthetic completed output ${turn + 1}\n")
            },
        )
        apply(
            "tool.complete",
            buildJsonObject {
                put("tool_id", toolId)
                put("name", "terminal")
                put("result_text", "completed")
            },
        )
        apply("message.complete")
    }

    listOf(0, 1, 2, 3, 200).forEach { step ->
        val event = streamingPerformanceEvent(
            runtimeSessionId = runtimeSessionId,
            toolId = "performance-active-0",
            cycle = 0,
            stepInCycle = step,
        )
        apply(event.type, event.payload ?: JsonObject(emptyMap()))
    }

    return SessionBrowserUiState(
        phase = SessionBrowserPhase.TRANSCRIPT,
        sessions = listOf(session),
        selectedSession = session,
        transcript = transcript,
        realtime = realtime,
        controlStatus = RealtimeControlStatus.OBSERVER,
        realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
    )
}

private fun streamingPerformanceEvent(
    runtimeSessionId: RuntimeSessionId,
    toolId: String,
    cycle: Long,
    stepInCycle: Int,
): GatewayEvent {
    val (type, payload) = when (stepInCycle) {
        0 -> "message.start" to JsonObject(emptyMap())
        1 -> "thinking.delta" to buildJsonObject {
            put(
                "text",
                "## Thinking\n\n- Cycle ${cycle + 1} is exercising **streaming Markdown**.\n",
            )
        }
        2 -> "tool.start" to buildJsonObject {
            put("tool_id", toolId)
            put("name", "terminal")
            put("args_text", "generate synthetic performance output --cycle ${cycle + 1}")
        }
        3 -> "tool.output.delta" to buildJsonObject {
            put("tool_id", toolId)
            put("name", "terminal")
            put("sequence", 1L)
            put(
                "text",
                (1..96).joinToString(separator = "\n", postfix = "\n") { line ->
                    "synthetic tool line ${line.toString().padStart(3, '0')} · cycle ${cycle + 1}"
                },
            )
        }
        in 4..199 -> "thinking.delta" to buildJsonObject {
            put("text", "- Verified synthetic frame `$stepInCycle` without sensitive data.\n")
        }
        in 200..399 -> "message.delta" to buildJsonObject {
            val text = if (stepInCycle == 200) {
                "## Live answer\n\n**Response:** the production renderer is receiving reducer deltas"
            } else {
                " · token-$stepInCycle"
            }
            put("text", text)
        }
        in 400..597 -> "tool.output.delta" to buildJsonObject {
            put("tool_id", toolId)
            put("name", "terminal")
            put("sequence", (stepInCycle - 398).toLong())
            put("text", "synthetic live output $stepInCycle\n")
        }
        598 -> "tool.complete" to buildJsonObject {
            put("tool_id", toolId)
            put("name", "terminal")
            put("result_text", "synthetic cycle complete")
        }
        else -> "message.complete" to JsonObject(emptyMap())
    }
    return GatewayEvent(
        type = type,
        runtimeSessionId = runtimeSessionId,
        payload = payload,
    )
}

private fun approvalReviewState(): SessionBrowserUiState {
    val sessionKey = SessionKey("mobile-ui")
    val session = SessionProjection(
        sessionKey = sessionKey,
        lineageRoot = sessionKey,
        lineageTip = sessionKey,
        parentSessionKey = null,
        title = "Hermes Agent",
        preview = "Native control review",
        source = "mobile-ui",
        model = "Hermes Agent",
        profile = null,
        cwd = null,
        gitBranch = null,
        startedAtEpochSeconds = 100.0,
        endedAtEpochSeconds = null,
        lastActiveEpochSeconds = 120.0,
        messageCount = 1,
        toolCallCount = 1,
        inputTokens = 10,
        outputTokens = 20,
        isActive = true,
        archived = false,
    )
    val transcript = SessionTranscript(
        sessionKey = sessionKey,
        lineageTip = sessionKey,
        messages = listOf(
            SessionMessageProjection(
                messageId = 1,
                role = "user",
                content = JsonPrimitive("Run the focused Android control tests and fix the first real failure."),
                timestampEpochSeconds = 100.0,
                reasoning = null,
                reasoningContent = null,
                reasoningDetails = null,
                toolCallId = null,
                toolCalls = null,
                toolName = null,
                displayKind = null,
                displayMetadata = null,
            ),
        ),
        pagination = TranscriptPagination(limit = 200, offset = 0, returned = 1),
    )
    val pending = SessionPendingInput.Approval(
        requestId = "approval-review",
        title = "Always allow this operation?",
        description = "This choice affects future sessions and requires explicit confirmation.",
        command = "./gradlew :app:testDebugUnitTest",
        choices = listOf(
            SessionApprovalChoice.ALLOW_ONCE,
            SessionApprovalChoice.ALLOW_SESSION,
            SessionApprovalChoice.ALLOW_ALWAYS,
            SessionApprovalChoice.DENY,
        ),
        expiresAtEpochMs = 9_000_000_000_000L,
    )
    val realtime = RealtimeSessionReducer().seed(
        transcript = transcript,
        runtimeSessionId = RuntimeSessionId("runtime-review"),
        connectionEpoch = 1,
    ).copy(
        running = true,
        streamingReasoningText = "I should inspect the current workspace and verify the first failing control test.",
        tools = listOf(
            LiveToolProjection(
                key = "tool-review",
                name = "terminal",
                status = LiveToolStatus.RUNNING,
                payload = buildJsonObject {
                    put("arguments", buildJsonObject {
                        put("command", "./gradlew :app:testDebugUnitTest")
                        put("workdir", "/workspace")
                    })
                    put("output", "Running focused tests…")
                },
            ),
        ),
    )
    return SessionBrowserUiState(
        phase = SessionBrowserPhase.TRANSCRIPT,
        sessions = listOf(session),
        selectedSession = session,
        transcript = transcript,
        realtime = realtime,
        control = ControlState(
            mode = ControlMode.Controller(
                SessionControlLease(
                    leaseId = SessionControlLeaseId("visual-review-only"),
                    expiresAtEpochMs = 9_000_000_000_000L,
                    controlRevision = 1L,
                    controllerKind = SessionControllerKind.MOBILE,
                    controllerLabel = "Hermes Mobile",
                    pendingInput = pending,
                ),
            ),
        ),
        controlStatus = RealtimeControlStatus.CONTROLLER,
        realtimeConnectionStatus = RealtimeConnectionStatus.LIVE,
        pendingInteraction = PendingInputInteractionState(
            requestId = pending.requestId,
            selectedChoiceId = SessionApprovalChoice.ALLOW_ALWAYS.wireValue,
            requiresConfirmation = true,
        ),
    )
}

private fun clarifyReviewState(): SessionBrowserUiState {
    val pending = SessionPendingInput.Clarify(
        requestId = "clarify-review",
        question = "Which deployment target should this run use?",
        choices = listOf(
            SessionClarifyChoice("staging", "Staging"),
            SessionClarifyChoice("production", "Production"),
        ),
        allowOther = true,
        expiresAtEpochMs = 9_000_000_000_000L,
    )
    return approvalReviewState()
        .retargetReviewSession(
            sessionKey = SessionKey("vivo-e2e"),
            title = "Release preparation",
            prompt = "Prepare the physical-device validation, but do not deploy until the target is explicit.",
            reasoning = "✓ Build debug APK\n✓ Verify control contract\n● Choose target environment",
            response = "The APK is ready. Select one target.",
            tools = emptyList(),
        )
        .withPending(
            pending = pending,
            interaction = PendingInputInteractionState(
                requestId = pending.requestId,
                otherDraft = "Internal QA cluster",
            ),
        )
}

private fun recoveryReviewState(): SessionBrowserUiState {
    val pending = SessionPendingInput.Approval(
        requestId = "recovery-review",
        title = "Run focused Android tests?",
        description = "The selected response is frozen until this retry completes.",
        command = "./gradlew :app:testDebugUnitTest",
        choices = listOf(
            SessionApprovalChoice.ALLOW_ONCE,
            SessionApprovalChoice.ALLOW_SESSION,
            SessionApprovalChoice.ALLOW_ALWAYS,
            SessionApprovalChoice.DENY,
        ),
        expiresAtEpochMs = 9_000_000_000_000L,
    )
    return approvalReviewState()
        .retargetReviewSession(
            sessionKey = SessionKey("mobile-ui"),
            title = "Hermes Agent",
            prompt = "Continue after reconnect without duplicating the pending action.",
            reasoning = "The runtime remains authoritative. The original response identity is preserved.",
            response = "No duplicate request was created while the control connection was unavailable.",
            tools = listOf(
                LiveToolProjection(
                    key = "approval-response-review",
                    name = "Approval response",
                    status = LiveToolStatus.INTERRUPTED,
                    payload = buildJsonObject {
                        put("output", "Response delivery unknown · retry identity preserved")
                    },
                ),
            ),
        )
        .withPending(
            pending = pending,
            interaction = PendingInputInteractionState(
                requestId = pending.requestId,
                selectedChoiceId = SessionApprovalChoice.ALLOW_ONCE.wireValue,
                outcome = PendingInputInteractionOutcome.RetryAvailable,
            ),
        )
}

private fun SessionBrowserUiState.retargetReviewSession(
    sessionKey: SessionKey,
    title: String,
    prompt: String,
    reasoning: String,
    response: String,
    tools: List<LiveToolProjection>,
): SessionBrowserUiState {
    val session = requireNotNull(selectedSession).copy(
        sessionKey = sessionKey,
        lineageRoot = sessionKey,
        lineageTip = sessionKey,
        title = title,
    )
    val sourceTranscript = requireNotNull(transcript)
    val reviewTranscript = sourceTranscript.copy(
        sessionKey = sessionKey,
        lineageTip = sessionKey,
        messages = sourceTranscript.messages.mapIndexed { index, message ->
            if (index == 0) message.copy(content = JsonPrimitive(prompt)) else message
        },
    )
    val reviewRealtime = RealtimeSessionReducer().seed(
        transcript = reviewTranscript,
        runtimeSessionId = RuntimeSessionId("runtime-${sessionKey.value}"),
        connectionEpoch = 1,
    ).copy(
        running = true,
        streamingAssistantText = response,
        streamingReasoningText = reasoning,
        tools = tools,
    )
    return copy(
        sessions = listOf(session),
        selectedSession = session,
        transcript = reviewTranscript,
        realtime = reviewRealtime,
    )
}

private fun SessionBrowserUiState.withPending(
    pending: SessionPendingInput,
    interaction: PendingInputInteractionState,
): SessionBrowserUiState = copy(
    control = ControlState(
        mode = ControlMode.Controller(
            SessionControlLease(
                leaseId = SessionControlLeaseId("visual-review-only"),
                expiresAtEpochMs = 9_000_000_000_000L,
                controlRevision = 1L,
                controllerKind = SessionControllerKind.MOBILE,
                controllerLabel = "Hermes Mobile",
                pendingInput = pending,
            ),
        ),
    ),
    pendingInteraction = interaction,
)
